import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import db

CMC_QUOTES_URL = "https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/latest"

COLLECTION = "crypto_watchlists"

CHECK_INTERVAL_SECONDS = 60

# Local hour (in the user's /settimezone offset, or UTC if they never set
# one) at which the daily price DM goes out.
DAILY_SEND_HOUR = 9

# A Discord select menu holds at most 25 options, and /editlist shows the
# whole watchlist in one, so that's the cap. (An embed also maxes out at 25
# fields, which the daily message uses one of per coin.)
MAX_COINS = 25

# Same reasoning as the calendar cog: let discord.py's rate limiter be the
# bottleneck rather than awaiting one DM at a time.
NOTIFY_CONCURRENCY = 20
BATCH_SIZE = 500

SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,15}$")


def next_daily_send(utc_offset: float, now: Optional[datetime] = None) -> datetime:
    """The next DAILY_SEND_HOUR:00 in the given UTC offset, strictly after now, as a UTC datetime."""
    tz = timezone(timedelta(hours=utc_offset))
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    send_local = now_local.replace(hour=DAILY_SEND_HOUR, minute=0, second=0, microsecond=0)
    if send_local <= now_local:
        send_local += timedelta(days=1)
    return send_local.astimezone(timezone.utc)


def format_price(price: float) -> str:
    if price >= 1:
        return f"${price:,.2f}"
    # Sub-dollar coins need more precision to be meaningful (e.g. $0.00001234).
    digits = f"{price:.10f}".rstrip("0")
    return "$" + (digits if len(digits.split(".")[1]) >= 2 else f"{price:.2f}")


def prices_embed(title: str, coins: List[str], quotes: Dict[str, dict], footer: str) -> discord.Embed:
    """One inline field per coin: price and 24h change, in watchlist order."""
    embed = discord.Embed(title=title, colour=discord.Colour.gold())
    for symbol in coins:
        coin = quotes.get(symbol)
        if coin is None:
            embed.add_field(name=symbol, value="No data available", inline=True)
            continue
        usd = coin["quote"]["USD"]
        change = usd.get("percent_change_24h") or 0
        arrow = "🟢" if change >= 0 else "🔴"
        embed.add_field(
            name=f"{symbol} · {coin['name']}",
            value=f"**{format_price(usd['price'])}**\n{arrow} {change:+.2f}% (24h)",
            inline=True,
        )
    embed.set_footer(text=footer)
    embed.timestamp = datetime.now(timezone.utc)
    return embed


class CoinMarketCapError(Exception):
    pass


# --- UI -------------------------------------------------------------------

class RemoveCoinsSelect(discord.ui.Select):
    def __init__(self, coins: List[str]):
        options = [discord.SelectOption(label=symbol, value=symbol) for symbol in coins]
        super().__init__(
            placeholder="Tick the coins to remove",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        doc = await db.find_one(COLLECTION, {"user_id": interaction.user.id})
        current = doc.get("coins", []) if doc else []
        removed = set(self.values)
        remaining = [symbol for symbol in current if symbol not in removed]

        await db.update_document(COLLECTION, {"user_id": interaction.user.id}, {"coins": remaining})

        self.view.stop()
        await interaction.response.edit_message(
            content=f"Removed: {', '.join(sorted(removed))}\n"
            + (f"Still tracking: {', '.join(remaining)}" if remaining else "Your watchlist is now empty."),
            view=None,
        )


class EditListView(discord.ui.View):
    def __init__(self, coins: List[str]):
        super().__init__(timeout=120)
        self.add_item(RemoveCoinsSelect(coins))


# --- Cog ------------------------------------------------------------------

class Crypto(commands.Cog):
    """Per-user crypto watchlists with a daily CoinMarketCap price DM."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api_key = os.getenv("CMC_API")
        self._session: Optional[aiohttp.ClientSession] = None
        self._notify_semaphore: Optional[asyncio.Semaphore] = None

    async def cog_load(self):
        if not self.api_key:
            print("CMC_API is not set - crypto commands will not be able to fetch prices")
        await db.ensure_index(COLLECTION, [("user_id", 1)])
        await db.ensure_index(COLLECTION, [("notify", 1), ("next_send_utc", 1)])
        self._session = aiohttp.ClientSession()
        self._notify_semaphore = asyncio.Semaphore(NOTIFY_CONCURRENCY)
        self.send_daily_prices.start()

    async def cog_unload(self):
        self.send_daily_prices.cancel()
        if self._session is not None:
            await self._session.close()

    # ------------------------------------------------------------- CMC API

    async def fetch_quotes(self, symbols: List[str]) -> Dict[str, dict]:
        """
        Look up the latest USD quotes for `symbols`. Returns {SYMBOL: coin}
        for every symbol CoinMarketCap recognises; unknown symbols are just
        absent. Raises CoinMarketCapError if the request itself fails.
        """
        if not self.api_key:
            raise CoinMarketCapError("CMC_API is not set")
        if not symbols:
            return {}

        params = {"symbol": ",".join(symbols), "convert": "USD", "skip_invalid": "true"}
        headers = {"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"}

        try:
            async with self._session.get(
                CMC_QUOTES_URL, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise CoinMarketCapError(f"request failed: {e}") from e

        status = payload.get("status", {})
        if resp.status != 200:
            # When every requested symbol is invalid CMC answers 400 rather
            # than an empty result, so treat that as "nothing found".
            if resp.status == 400 and "symbol" in (status.get("error_message") or "").lower():
                return {}
            raise CoinMarketCapError(f"HTTP {resp.status}: {status.get('error_message')}")

        quotes = {}
        for symbol, matches in (payload.get("data") or {}).items():
            if not matches:
                continue
            # Several coins can share a ticker; take the highest ranked one.
            best = min(matches, key=lambda c: (c.get("cmc_rank") is None, c.get("cmc_rank") or 0))
            quotes[symbol.upper()] = best
        return quotes

    # ------------------------------------------------------------ commands

    @app_commands.command(name="addcoin", description="Add a cryptocurrency to your daily price watchlist")
    @app_commands.describe(ticker="The coin's ticker symbol, e.g. BTC, ETH, SOL")
    async def addcoin(self, interaction: discord.Interaction, ticker: str):
        symbol = ticker.strip().upper().lstrip("$")
        if not SYMBOL_RE.match(symbol):
            await interaction.response.send_message(
                "That doesn't look like a ticker symbol - use letters/numbers only, e.g. BTC.",
                ephemeral=True,
            )
            return

        doc = await db.find_one(COLLECTION, {"user_id": interaction.user.id})
        coins = doc.get("coins", []) if doc else []

        if symbol in coins:
            await interaction.response.send_message(f"You're already tracking **{symbol}**.", ephemeral=True)
            return
        if len(coins) >= MAX_COINS:
            await interaction.response.send_message(
                f"You can track at most {MAX_COINS} coins. Remove some with /editlist first.",
                ephemeral=True,
            )
            return

        # The API call can take a moment - defer so the interaction doesn't time out.
        await interaction.response.defer(ephemeral=True)

        try:
            quotes = await self.fetch_quotes([symbol])
        except CoinMarketCapError as e:
            print(f"CoinMarketCap lookup for {symbol} failed: {e}")
            await interaction.followup.send("Couldn't reach CoinMarketCap right now - try again later.", ephemeral=True)
            return

        coin = quotes.get(symbol)
        if coin is None:
            await interaction.followup.send(f"CoinMarketCap doesn't know a coin with ticker **{symbol}**.", ephemeral=True)
            return

        coins.append(symbol)
        if doc is None:
            # First coin: turn the daily DM on by default.
            settings = await db.find_one("user_settings", {"user_id": interaction.user.id})
            offset = settings["utc_offset"] if settings else 0
            await db.insert_document(COLLECTION, {
                "user_id": interaction.user.id,
                "coins": coins,
                "notify": True,
                "next_send_utc": next_daily_send(offset),
            })
            notif_note = f"\nDaily price DMs are **on** ({DAILY_SEND_HOUR}:00 your time) - use /togglenotif to turn them off."
        else:
            await db.update_document(COLLECTION, {"user_id": interaction.user.id}, {"coins": coins})
            notif_note = "" if doc.get("notify") else "\nDaily price DMs are currently **off** - use /togglenotif to turn them on."

        price = coin["quote"]["USD"]["price"]
        await interaction.followup.send(
            f"Added **{symbol}** ({coin['name']}), currently {format_price(price)}.{notif_note}",
            ephemeral=True,
        )

    @app_commands.command(name="editlist", description="Remove coins from your crypto watchlist")
    async def editlist(self, interaction: discord.Interaction):
        doc = await db.find_one(COLLECTION, {"user_id": interaction.user.id})
        coins = doc.get("coins", []) if doc else []

        if not coins:
            await interaction.response.send_message("Your watchlist is empty - add coins with /addcoin.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Select the coins you want to remove:",
            view=EditListView(coins),
            ephemeral=True,
        )

    @app_commands.command(name="coinlist", description="Show the coins on your watchlist and their current prices")
    async def coinlist(self, interaction: discord.Interaction):
        doc = await db.find_one(COLLECTION, {"user_id": interaction.user.id})
        coins = doc.get("coins", []) if doc else []

        if not coins:
            await interaction.response.send_message("Your watchlist is empty - add coins with /addcoin.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            quotes = await self.fetch_quotes(coins)
        except CoinMarketCapError as e:
            print(f"CoinMarketCap lookup for /coinlist failed: {e}")
            await interaction.followup.send(
                f"Couldn't reach CoinMarketCap right now. Your coins: {', '.join(coins)}",
                ephemeral=True,
            )
            return

        notif = "on" if doc.get("notify") else "off"
        embed = prices_embed(
            "Your crypto watchlist",
            coins,
            quotes,
            footer=f"Data from CoinMarketCap · Daily DMs are {notif} (/togglenotif)",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="togglenotif", description="Turn your daily crypto price DMs on or off")
    async def togglenotif(self, interaction: discord.Interaction):
        doc = await db.find_one(COLLECTION, {"user_id": interaction.user.id})
        enable = not (doc or {}).get("notify", False)

        update = {"user_id": interaction.user.id, "notify": enable}
        if enable:
            settings = await db.find_one("user_settings", {"user_id": interaction.user.id})
            offset = settings["utc_offset"] if settings else 0
            update["next_send_utc"] = next_daily_send(offset)
        if doc is None:
            update["coins"] = []
        await db.upsert_document(COLLECTION, {"user_id": interaction.user.id}, update)

        if not enable:
            await interaction.response.send_message("Daily crypto price DMs turned **off**.", ephemeral=True)
            return

        ts = int(update["next_send_utc"].timestamp())
        text = f"Daily crypto price DMs turned **on**. Next one: <t:{ts}:F>."
        if not (doc or {}).get("coins"):
            text += "\nYour watchlist is empty - add coins with /addcoin."
        await interaction.response.send_message(text, ephemeral=True)

    # ------------------------------------------------------ background loop

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def send_daily_prices(self):
        # See calendar.py: DMs aren't shard-specific, so only one shard sends.
        if getattr(self.bot, "shard_id", None) not in (None, 0):
            return
        if not self.api_key:
            return

        now = datetime.now(timezone.utc)
        cursor = db.iter_documents(COLLECTION, {"notify": True, "next_send_utc": {"$lte": now}}, batch_size=BATCH_SIZE)
        batch = []

        async for doc in cursor:
            batch.append(doc)
            if len(batch) >= BATCH_SIZE:
                await self._flush_batch(batch, now)
                batch = []

        if batch:
            await self._flush_batch(batch, now)

    @send_daily_prices.before_loop
    async def before_send_daily_prices(self):
        await self.bot.wait_until_ready()

    async def _flush_batch(self, batch: list, now: datetime):
        # One API call for every distinct coin in the batch, rather than one
        # per user - keeps us well inside CoinMarketCap's credit limits.
        symbols = sorted({symbol for doc in batch for symbol in doc.get("coins", [])})
        try:
            quotes = await self.fetch_quotes(symbols)
        except CoinMarketCapError as e:
            # Leave next_send_utc alone so this batch is retried next tick.
            print(f"Daily crypto prices: CoinMarketCap fetch failed: {e}")
            return

        # Reschedule from each user's *current* timezone, so a later
        # /settimezone is picked up on the next send.
        user_ids = [doc["user_id"] for doc in batch]
        offsets = {
            s["user_id"]: s["utc_offset"]
            async for s in db.iter_documents("user_settings", {"user_id": {"$in": user_ids}})
        }
        # Mark before sending: a crash in between means a missed DM, never a duplicate.
        await db.bulk_update(
            COLLECTION,
            [(doc["_id"], {"next_send_utc": next_daily_send(offsets.get(doc["user_id"], 0), now)}) for doc in batch],
        )

        await asyncio.gather(
            *(self._notify_bounded(doc, quotes) for doc in batch if doc.get("coins")),
            return_exceptions=True,
        )

    async def _notify_bounded(self, doc: dict, quotes: Dict[str, dict]):
        async with self._notify_semaphore:
            await self._notify(doc, quotes)

    async def _notify(self, doc: dict, quotes: Dict[str, dict]):
        try:
            user = self.bot.get_user(doc["user_id"]) or await self.bot.fetch_user(doc["user_id"])
        except discord.NotFound:
            return

        embed = prices_embed(
            "Your daily crypto prices",
            doc["coins"],
            quotes,
            footer="Data from CoinMarketCap · /togglenotif to stop these DMs",
        )

        try:
            await user.send(embed=embed)
        except discord.Forbidden:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Crypto(bot))
