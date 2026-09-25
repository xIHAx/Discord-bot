import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db

CHECK_INTERVAL_SECONDS = 30

# How many DMs to have in flight at once. discord.py's HTTP client already
# rate-limits the actual traffic per-route, so this isn't about respecting
# Discord's limits (that happens regardless) - it's about not being
# needlessly serial: awaiting one send at a time means throughput is capped
# by round-trip latency times the number of due users, which gets slow fast
# at scale. Firing many concurrently lets the rate limiter (not our own
# `await`) be the actual bottleneck.
NOTIFY_CONCURRENCY = 20

# How many due documents we buffer before flushing one bulk "mark done"
# write, instead of one write per document. Also caps how many documents
# we ever hold in memory at once, so a huge backlog can't spike RAM.
BATCH_SIZE = 500


class Calendar(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._notify_semaphore: Optional[asyncio.Semaphore] = None

    async def cog_load(self):
        # Indexes matching the queries check_events() runs below.
        await db.ensure_index("events", [("reminder_sent", 1), ("remind_time_utc", 1)])
        await db.ensure_index("events", [("event_sent", 1), ("event_time_utc", 1)])
        # Created here (inside the bot's running event loop) rather than in
        # __init__, since asyncio.Semaphore should be created where it'll run.
        self._notify_semaphore = asyncio.Semaphore(NOTIFY_CONCURRENCY)
        self.check_events.start()

    def cog_unload(self):
        self.check_events.cancel()

    # ---------------------------------------------------------------- timezone

    @app_commands.command(name="settimezone", description="Set your UTC offset, e.g. 8 for UTC+8, -5 for UTC-5")
    @app_commands.describe(utc_offset="Hours offset from UTC. Decimals allowed, e.g. 5.5 for UTC+5:30")
    async def settimezone(self, interaction: discord.Interaction, utc_offset: float):
        if not -12 <= utc_offset <= 14:
            await interaction.response.send_message(
                "That doesn't look like a real UTC offset (must be between -12 and +14).",
                ephemeral=True,
            )
            return

        await db.upsert_document(
            "user_settings",
            {"user_id": interaction.user.id},
            {"user_id": interaction.user.id, "utc_offset": utc_offset},
        )

        sign = "+" if utc_offset >= 0 else ""
        await interaction.response.send_message(f"Timezone set to UTC{sign}{utc_offset}.", ephemeral=True)

    # events

    @app_commands.command(name="createevent", description="Create an event with a reminder")
    @app_commands.describe(
        time="24-hour time, e.g. 14:30",
        title="Event title",
        description="Event description",
        remind_before_hours="How many hours before the event to remind you. Decimals allowed, e.g. 1.5. Use 0 for no separate reminder.",
        date="Optional date as YYYY-MM-DD. Defaults to today, or tomorrow if that time has already passed today.",
    )
    async def createevent(
        self,
        interaction: discord.Interaction,
        time: str,
        title: str,
        description: str,
        remind_before_hours: float,
        date: Optional[str] = None,
    ):
        settings = await db.find_one("user_settings", {"user_id": interaction.user.id})
        if settings is None:
            await interaction.response.send_message(
                "Set your timezone first with /settimezone.",
                ephemeral=True,
            )
            return

        try:
            hour_str, minute_str = time.split(":")
            hour, minute = int(hour_str), int(minute_str)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "Time must be 24-hour HH:MM, e.g. 09:05 or 21:45.",
                ephemeral=True,
            )
            return

        if remind_before_hours < 0:
            await interaction.response.send_message("remind_before_hours can't be negative.", ephemeral=True)
            return

        user_tz = timezone(timedelta(hours=settings["utc_offset"]))
        now_local = datetime.now(user_tz)

        if date:
            try:
                y, m, d = (int(part) for part in date.split("-"))
                event_local = datetime(y, m, d, hour, minute, tzinfo=user_tz)
            except ValueError:
                await interaction.response.send_message("Date must be YYYY-MM-DD.", ephemeral=True)
                return
        else:
            event_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if event_local <= now_local:
                event_local += timedelta(days=1)

        event_time_utc = event_local.astimezone(timezone.utc)
        remind_time_utc = event_time_utc - timedelta(hours=remind_before_hours)

        await db.insert_document("events", {
            "user_id": interaction.user.id,
            "title": title,
            "description": description,
            "event_time_utc": event_time_utc,
            "remind_time_utc": remind_time_utc,
            "remind_before_hours": remind_before_hours,
            # If there's no separate reminder (0 hours before), don't send a
            # redundant "reminder" right on top of the event ping.
            "reminder_sent": remind_before_hours == 0,
            "event_sent": False,
        })

        event_ts = int(event_time_utc.timestamp())
        remind_ts = int(remind_time_utc.timestamp())
        await interaction.response.send_message(
            f"Created **{title}**, happening <t:{event_ts}:F> (<t:{event_ts}:R>)."
            + (f"\nReminder set for <t:{remind_ts}:R>." if remind_before_hours > 0 else ""),
            ephemeral=True,
        )

    # ------------------------------------------------------------ background loop

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def check_events(self):
        # If this bot ever runs sharded, DMs aren't shard-specific - every
        # shard sees the same guild-less user IDs, so running this loop on
        # every shard would send each reminder once per shard. Only shard 0
        # (or an unsharded bot, where shard_id is None) should run it.
        if getattr(self.bot, "shard_id", None) not in (None, 0):
            return

        now = datetime.now(timezone.utc)
        await self._process_due(
            {"reminder_sent": False, "remind_time_utc": {"$lte": now}},
            sent_field="reminder_sent",
            is_reminder=True,
        )
        await self._process_due(
            {"event_sent": False, "event_time_utc": {"$lte": now}},
            sent_field="event_sent",
            is_reminder=False,
        )

    @check_events.before_loop
    async def before_check_events(self):
        # Don't touch the API or DB until the bot is fully logged in and ready.
        await self.bot.wait_until_ready()

    async def _process_due(self, query: dict, sent_field: str, is_reminder: bool):
        """
        Stream every document matching `query` in batches (never loading an
        unbounded result set into memory), and for each batch:
          1. bulk-mark the whole batch as sent, in one write
          2. only then send the DMs, many concurrently

        Marking-before-sending means a crash between the two can cause a
        missed reminder, never a duplicate one - the safer failure mode for
        something that pings a person's DMs.
        """
        cursor = db.iter_documents("events", query, batch_size=BATCH_SIZE)
        batch = []

        async for event in cursor:
            batch.append(event)
            if len(batch) >= BATCH_SIZE:
                await self._flush_batch(batch, sent_field, is_reminder)
                batch = []

        if batch:
            await self._flush_batch(batch, sent_field, is_reminder)

    async def _flush_batch(self, batch: list, sent_field: str, is_reminder: bool):
        await db.bulk_set("events", [event["_id"] for event in batch], {sent_field: True})
        await asyncio.gather(
            *(self._notify_bounded(event, is_reminder) for event in batch),
            return_exceptions=True,  # one bad send shouldn't cancel the rest of the batch
        )

    async def _notify_bounded(self, event: dict, is_reminder: bool):
        async with self._notify_semaphore:
            await self._notify(event, is_reminder)

    async def _notify(self, event: dict, is_reminder: bool):
        try:
            user = self.bot.get_user(event["user_id"]) or await self.bot.fetch_user(event["user_id"])
        except discord.NotFound:
            return

        if is_reminder:
            hours = event["remind_before_hours"]
            unit = "hour" if hours == 1 else "hours"
            text = f"**Reminder:** \"{event['title']}\" starts in {hours} {unit}.\n{event['description']}"
        else:
            text = f"**It's time!** \"{event['title']}\" is starting now.\n{event['description']}"

        try:
            await user.send(text)
        except discord.Forbidden:
            # User has DMs from bot users/server members turned off - nothing more we can do.
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Calendar(bot))
