import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import discord
from bson import ObjectId
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

# Snooze choices offered on a reminder DM. Only the ones that still land
# before the event starts are shown.
SNOOZE_MINUTES = (15, 30, 60)

# How many events /reminderlist and /pastevents show before "...and N more".
# Keeps the list inside an embed description's 4096-character limit.
MAX_LISTED_EVENTS = 25


# --- helpers (also used by the crypto cog's daily summary) ----------------

def as_utc(dt: datetime) -> datetime:
    """Mongo hands datetimes back naive (but in UTC) - make them aware so
    .timestamp() and comparisons with datetime.now(timezone.utc) are right."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def parse_time_of_day(text: str) -> Optional[Tuple[int, int]]:
    """'14:30' -> (14, 30). None if it isn't a valid 24-hour HH:MM."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text)
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def event_line(event: dict) -> str:
    ts = int(as_utc(event["event_time_utc"]).timestamp())
    return f"<t:{ts}:f> (<t:{ts}:R>) · **{event['title'][:100]}**"


def events_text(events: List[dict], limit: int, more_hint: str = "") -> str:
    """One line per event, capped at `limit` with an '...and N more' line."""
    lines = [event_line(e) for e in events[:limit]]
    if len(events) > limit:
        lines.append(f"…and {len(events) - limit} more{more_hint}")
    return "\n".join(lines)


def upcoming_query(user_id: int) -> dict:
    return {"user_id": user_id, "event_sent": False}


def past_query(user_id: int) -> dict:
    # event_sent rather than just the time, so an event that's due but whose
    # "it's time" DM hasn't gone out yet isn't counted (or cleared) as past.
    return {"user_id": user_id, "event_sent": True}


# --- UI -------------------------------------------------------------------

class SnoozeButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"snooze:(?P<event_id>[0-9a-f]{24}):(?P<minutes>[0-9]+)",
):
    """
    A snooze button on a reminder DM. It's a DynamicItem - its state lives in
    the custom_id - so buttons on old DMs keep working after the bot restarts.
    """

    def __init__(self, event_id: str, minutes: int):
        self.event_id = event_id
        self.minutes = minutes
        label = f"Snooze {minutes} min" if minutes < 60 else f"Snooze {minutes // 60} h"
        super().__init__(discord.ui.Button(
            label=label,
            emoji="💤",
            style=discord.ButtonStyle.secondary,
            custom_id=f"snooze:{event_id}:{minutes}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        return cls(match["event_id"], int(match["minutes"]))

    async def callback(self, interaction: discord.Interaction):
        event = await db.find_one("events", {"_id": ObjectId(self.event_id), "user_id": interaction.user.id})
        if event is None:
            await interaction.response.edit_message(content=f"{interaction.message.content}\n*This event no longer exists.*", view=None)
            return

        now = datetime.now(timezone.utc)
        event_time = as_utc(event["event_time_utc"])
        snooze_until = now + timedelta(minutes=self.minutes)
        if snooze_until >= event_time:
            await interaction.response.send_message(
                f"That's past the start time - it begins <t:{int(event_time.timestamp())}:R>.",
            )
            return

        # Re-arm the reminder; check_events() will send it again at the new
        # time. The snoozed reminder replaces the "It's time!" DM, so mute
        # that - the event is still marked sent at its start time (see
        # _notify), so it moves to /pastevents as usual.
        await db.update_document(
            "events",
            {"_id": event["_id"]},
            {"remind_time_utc": snooze_until, "reminder_sent": False, "start_notification_muted": True},
        )
        await interaction.response.edit_message(
            content=f"{interaction.message.content}\n💤 Snoozed - I'll remind you again <t:{int(snooze_until.timestamp())}:R>. "
            "You won't get a separate message when it starts.",
            view=None,
        )


def snooze_view(event: dict) -> Optional[discord.ui.View]:
    """Snooze buttons for a reminder DM, or None if every option would land after the event starts."""
    now = datetime.now(timezone.utc)
    event_time = as_utc(event["event_time_utc"])
    view = discord.ui.View(timeout=None)
    for minutes in SNOOZE_MINUTES:
        if now + timedelta(minutes=minutes) < event_time:
            view.add_item(SnoozeButton(str(event["_id"]), minutes))
    return view if view.children else None


class ClearPastView(discord.ui.View):
    def __init__(self, user_id: int, count: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.confirm.label = f"Delete {count} past event{'s' if count != 1 else ''}"

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        deleted = await db.delete_documents("events", past_query(self.user_id))
        self.stop()
        await interaction.response.edit_message(
            content=f"Deleted {deleted} past event{'s' if deleted != 1 else ''}.", view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Nothing deleted.", view=None)


class Calendar(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._notify_semaphore: Optional[asyncio.Semaphore] = None

    async def cog_load(self):
        # Indexes matching the queries check_events() runs below.
        await db.ensure_index("events", [("reminder_sent", 1), ("remind_time_utc", 1)])
        await db.ensure_index("events", [("event_sent", 1), ("event_time_utc", 1)])
        # ...and the per-user listing queries.
        await db.ensure_index("events", [("user_id", 1), ("event_sent", 1), ("event_time_utc", 1)])
        # Created here (inside the bot's running event loop) rather than in
        # __init__, since asyncio.Semaphore should be created where it'll run.
        self._notify_semaphore = asyncio.Semaphore(NOTIFY_CONCURRENCY)
        # Lets snooze buttons on any reminder DM - including ones sent before
        # a restart - be routed to SnoozeButton.callback.
        self.bot.add_dynamic_items(SnoozeButton)
        self.check_events.start()

    def cog_unload(self):
        self.check_events.cancel()
        self.bot.remove_dynamic_items(SnoozeButton)

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
        date="Date as YYYY-MM-DD, e.g. 2026-10-03",
        time="24-hour time, e.g. 14:30",
        title="Event title",
        description="Event description",
        remind_before_hours="How many hours before the event to remind you. Decimals allowed, e.g. 1.5. Use 0 for no separate reminder.",
    )
    async def createevent(
        self,
        interaction: discord.Interaction,
        date: str,
        time: str,
        title: str,
        description: str,
        remind_before_hours: float,
    ):
        settings = await db.find_one("user_settings", {"user_id": interaction.user.id})
        if settings is None:
            await interaction.response.send_message(
                "Set your timezone first with /settimezone.",
                ephemeral=True,
            )
            return

        parsed = parse_time_of_day(time)
        if parsed is None:
            await interaction.response.send_message(
                "Time must be 24-hour HH:MM, e.g. 09:05 or 21:45.",
                ephemeral=True,
            )
            return
        hour, minute = parsed

        if remind_before_hours < 0:
            await interaction.response.send_message("remind_before_hours can't be negative.", ephemeral=True)
            return

        user_tz = timezone(timedelta(hours=settings["utc_offset"]))
        try:
            y, m, d = (int(part) for part in date.strip().split("-"))
            event_local = datetime(y, m, d, hour, minute, tzinfo=user_tz)
        except ValueError:
            await interaction.response.send_message("Date must be a real date as YYYY-MM-DD, e.g. 2026-10-03.", ephemeral=True)
            return

        if event_local <= datetime.now(user_tz):
            await interaction.response.send_message("That date and time has already passed.", ephemeral=True)
            return

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

    # listing / clearing

    async def _find_events(self, query: dict, newest_first: bool = False) -> List[dict]:
        cursor = db.iter_documents("events", query, sort=[("event_time_utc", -1 if newest_first else 1)])
        return [event async for event in cursor]

    @app_commands.command(name="reminderlist", description="Show all your upcoming events and reminders")
    async def reminderlist(self, interaction: discord.Interaction):
        events = await self._find_events(upcoming_query(interaction.user.id))
        if not events:
            await interaction.response.send_message("You have no upcoming events. Create one with /createevent.", ephemeral=True)
            return

        lines = []
        for event in events[:MAX_LISTED_EVENTS]:
            line = event_line(event)
            if not event["reminder_sent"]:
                line += f" · 🔔 <t:{int(as_utc(event['remind_time_utc']).timestamp())}:R>"
            lines.append(line)
        if len(events) > MAX_LISTED_EVENTS:
            lines.append(f"…and {len(events) - MAX_LISTED_EVENTS} more")

        embed = discord.Embed(title=f"Your upcoming events ({len(events)})", description="\n".join(lines), colour=discord.Colour.purple())
        embed.set_footer(text="🔔 = when your reminder goes off")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="pastevents", description="Show your events that have already happened")
    async def pastevents(self, interaction: discord.Interaction):
        events = await self._find_events(past_query(interaction.user.id), newest_first=True)
        if not events:
            await interaction.response.send_message("You have no past events.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Your past events ({len(events)})",
            description=events_text(events, MAX_LISTED_EVENTS),
            colour=discord.Colour.dark_grey(),
        )
        embed.set_footer(text="Newest first · /clearpast to delete these")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clearpast", description="Delete all of your events that have already happened")
    async def clearpast(self, interaction: discord.Interaction):
        count = len(await self._find_events(past_query(interaction.user.id)))
        if not count:
            await interaction.response.send_message("You have no past events to clear.", ephemeral=True)
            return

        await interaction.response.send_message(
            "This permanently deletes your past events. Upcoming events aren't affected.",
            view=ClearPastView(interaction.user.id, count),
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
        if not is_reminder and event.get("start_notification_muted"):
            # The user snoozed this event's reminder - that replaces the start DM.
            return

        try:
            user = self.bot.get_user(event["user_id"]) or await self.bot.fetch_user(event["user_id"])
        except discord.NotFound:
            return

        view = None
        if is_reminder:
            # A relative timestamp rather than remind_before_hours, since a
            # snoozed reminder no longer goes off that far ahead.
            event_ts = int(as_utc(event["event_time_utc"]).timestamp())
            text = f"**Reminder:** \"{event['title']}\" starts <t:{event_ts}:R> (<t:{event_ts}:t>).\n{event['description']}"
            view = snooze_view(event)
        else:
            text = f"**It's time!** \"{event['title']}\" is starting now.\n{event['description']}"

        try:
            await user.send(text, view=view)
        except discord.Forbidden:
            # User has DMs from bot users/server members turned off - nothing more we can do.
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Calendar(bot))
