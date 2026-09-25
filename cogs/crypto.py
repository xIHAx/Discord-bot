import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp
import discord
import yfinance as yf
from discord import app_commands
from discord.ext import commands, tasks

import db

# yfinance logs every unknown ticker at ERROR level; we report those to the
# user ourselves, so keep them out of the console.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

CMC_QUOTES_URL = "https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/latest"
CMC_FEAR_GREED_URL = "https://pro-api.coinmarketcap.com/v3/fear-and-greed/latest"

COLLECTION = "crypto_watchlists"

CHECK_INTERVAL_SECONDS = 60

# Local hour (in the user's /settimezone offset, or UTC if they never set
# one) at which the daily price DM goes out.
DAILY_SEND_HOUR = 9

# A Discord select menu holds at most 25 options, and /editlist shows each
# list in one, so that's the cap per list. (An embed also maxes out at 25
# fields, which the daily message uses one of per coin/stock.)
MAX_COINS = 25
MAX_STOCKS = 25

# Same reasoning as the calendar cog: let discord.py's rate limiter be the
# bottleneck rather than awaiting one DM at a time.
NOTIFY_CONCURRENCY = 20
BATCH_SIZE = 500

COIN_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,15}$")
# Yahoo tickers can carry an exchange suffix (SHOP.TO), a class (BRK-B), or
# be an index (^GSPC) or future/FX pair (GC=F, EURUSD=X).
STOCK_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-^=]{1,15}$")

# CoinMarketCap's Fear & Greed index is 0-100; these are its own bands.
# (exclusive upper bound, emoji, embed colour)
SENTIMENT_BANDS = [
    (20, "🔴", 0xE74C3C),   # Extreme fear
    (40, "🟠", 0xE67E22),   # Fear
    (60, "🟡", 0xF1C40F),   # Neutral
    (80, "🟢", 0x2ECC71),   # Greed
    (101, "💚", 0x1E8449),  # Extreme greed
]


def next_daily_send(utc_offset: float, now: Optional[datetime] = None) -> datetime:
    """The next DAILY_SEND_HOUR:00 in the given UTC offset, strictly after now, as a UTC datetime."""
    tz = timezone(timedelta(hours=utc_offset))
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    send_local = now_local.replace(hour=DAILY_SEND_HOUR, minute=0, second=0, microsecond=0)
    if send_local <= now_local:
        send_local += timedelta(days=1)
    return send_local.astimezone(timezone.utc)


def parse_symbols(text: str) -> List[str]:
    """Split a comma-separated ticker list, normalise, and de-duplicate, keeping the order typed."""
    return list(dict.fromkeys(
        t.strip().upper().lstrip("$") for t in text.split(",") if t.strip().lstrip("$")
    ))


def format_price(price: float) -> str:
    if price >= 1:
        return f"${price:,.2f}"
    # Sub-dollar coins need more precision to be meaningful (e.g. $0.00001234).
    digits = f"{price:.10f}".rstrip("0")
    return "$" + (digits if len(digits.split(".")[1]) >= 2 else f"{price:.2f}")


def format_money(price: float, currency: str) -> str:
    """format_price for USD; otherwise the amount followed by the currency code, e.g. 205.30 CAD."""
    if currency == "USD":
        return format_price(price)
    return f"{format_price(price)[1:]} {currency}"


def change_line(change: Optional[float], period: str) -> str:
    if change is None:
        return f"– ({period})"
    arrow = "🟢" if change >= 0 else "🔴"
    return f"{arrow} {change:+.2f}% ({period})"


def crypto_embed(coins: List[str], quotes: Dict[str, dict], sentiment: Optional[dict]) -> discord.Embed:
    """One inline field per coin (price and 24h change), headed by the market's Fear & Greed reading."""
    embed = discord.Embed(title="Your daily crypto prices", colour=discord.Colour.gold())
    if sentiment:
        value = sentiment["value"]
        emoji, colour = next((e, c) for bound, e, c in SENTIMENT_BANDS if value < bound)
        embed.description = f"**Market sentiment:** {emoji} {value}/100 · {sentiment['label']}"
        embed.colour = discord.Colour(colour)

    for symbol in coins:
        coin = quotes.get(symbol)
        if coin is None:
            embed.add_field(name=symbol, value="No data available", inline=True)
            continue
        usd = coin["quote"]["USD"]
        embed.add_field(
            name=f"{symbol} · {coin['name']}",
            value=f"**{format_price(usd['price'])}**\n{change_line(usd.get('percent_change_24h'), '24h')}",
            inline=True,
        )
    embed.set_footer(text="Data from CoinMarketCap")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def stocks_embed(stocks: List[dict], quotes: Dict[str, dict]) -> discord.Embed:
    """One inline field per stock: price in its own currency and change since the previous close."""
    embed = discord.Embed(title="Your daily stock prices", colour=discord.Colour.blue())
    for stock in stocks:
        symbol = stock["symbol"]
        quote = quotes.get(symbol)
        if quote is None:
            embed.add_field(name=symbol, value="No data available", inline=True)
            continue
        embed.add_field(
            name=f"{symbol} · {stock['name']}",
            value=f"**{format_money(quote['price'], stock['currency'])}**\n{change_line(quote['change_pct'], '1d')}",
            inline=True,
        )
    embed.set_footer(text="Data from Yahoo Finance")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def daily_embeds(
    coins: List[str],
    quotes: Dict[str, dict],
    sentiment: Optional[dict],
    stocks: List[dict],
    stock_quotes: Dict[str, dict],
) -> List[discord.Embed]:
    """The daily price DM - also what /showlist sends on demand. One embed per non-empty list."""
    embeds = []
    if coins:
        embeds.append(crypto_embed(coins, quotes, sentiment))
    if stocks:
        embeds.append(stocks_embed(stocks, stock_quotes))
    if embeds:
        embeds[-1].set_footer(text=f"{embeds[-1].footer.text} · /togglenotif to stop these DMs")
    return embeds


def add_report(
    added_lines: List[str],
    already: List[str],
    unknown: List[str],
    source: str,
    malformed: List[str],
    no_room: List[str],
    cap: int,
    notif_note: str,
) -> str:
    """The reply for /addcoin and /addstock: what happened to each ticker."""
    lines = []
    if added_lines:
        lines.append("**Added:**")
        lines += added_lines
    if already:
        lines.append(f"Already tracking: {', '.join(already)}")
    if unknown:
        lines.append(f"{source} doesn't know: {', '.join(unknown)}")
    if malformed:
        lines.append(f"Not valid ticker symbols: {', '.join(malformed)}")
    if no_room:
        lines.append(f"Not added, list is full ({cap} max): {', '.join(no_room)}")
    if notif_note:
        lines.append(notif_note)
    return "\n".join(lines)


class CoinMarketCapError(Exception):
    pass


class StockDataError(Exception):
    pass


# --- yfinance (blocking - always called via asyncio.to_thread) ------------

def _download_stock_quotes(symbols: List[str]) -> Dict[str, dict]:
    """{SYMBOL: {"price", "change_pct"}} for every symbol Yahoo has recent prices for."""
    df = yf.download(
        symbols, period="5d", interval="1d", group_by="ticker",
        auto_adjust=False, progress=False, threads=True,
    )
    quotes = {}
    if df is None or df.empty:
        return quotes

    available = set(df.columns.get_level_values(0))
    for symbol in symbols:
        # Unknown tickers come back as all-NaN columns (or none at all), not an error.
        if symbol not in available:
            continue
        closes = df[symbol]["Close"].dropna()
        if closes.empty:
            continue
        price = float(closes.iloc[-1])
        previous = float(closes.iloc[-2]) if len(closes) >= 2 else 0
        quotes[symbol] = {
            "price": price,
            "change_pct": (price / previous - 1) * 100 if previous else None,
        }
    return quotes


def _stock_meta(symbol: str) -> dict:
    """Display name and trading currency for a ticker, as stored in the watchlist."""
    ticker = yf.Ticker(symbol)
    try:
        info = ticker.info or {}
    except Exception:
        info = {}
    currency = info.get("currency")
    if not currency:
        try:
            currency = ticker.fast_info["currency"]
        except Exception:
            currency = "USD"
    return {
        "symbol": symbol,
        "name": info.get("shortName") or info.get("longName") or symbol,
        "currency": currency or "USD",
    }


# --- UI -------------------------------------------------------------------

class WatchlistSelect(discord.ui.Select):
    def __init__(self, placeholder: str, options: List[discord.SelectOption], row: int):
        # min_values=0 so a tick can be undone before pressing Remove.
        super().__init__(placeholder=placeholder, min_values=0, max_values=len(options), options=options, row=row)

    async def callback(self, interaction: discord.Interaction):
        # Nothing happens until the Remove button - just acknowledge the change.
        await interaction.response.defer()


class EditListView(discord.ui.View):
    def __init__(self, coins: List[str], stocks: List[dict]):
        super().__init__(timeout=180)
        self.coin_select = self.stock_select = None
        if coins:
            self.coin_select = WatchlistSelect(
                "Crypto: tick coins to remove",
                [discord.SelectOption(label=symbol, value=symbol) for symbol in coins],
                row=0,
            )
            self.add_item(self.coin_select)
        if stocks:
            self.stock_select = WatchlistSelect(
                "Stocks: tick stocks to remove",
                [
                    discord.SelectOption(label=s["symbol"], value=s["symbol"], description=s["name"][:100])
                    for s in stocks
                ],
                row=1,
            )
            self.add_item(self.stock_select)

    @discord.ui.button(label="Remove selected", style=discord.ButtonStyle.danger, row=2)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        coins_out = set(self.coin_select.values) if self.coin_select else set()
        stocks_out = set(self.stock_select.values) if self.stock_select else set()
        if not coins_out and not stocks_out:
            await interaction.response.send_message("Tick at least one coin or stock first.", ephemeral=True)
            return

        doc = await db.find_one(COLLECTION, {"user_id": interaction.user.id}) or {}
        coins = [c for c in doc.get("coins", []) if c not in coins_out]
        stocks = [s for s in doc.get("stocks", []) if s["symbol"] not in stocks_out]
        await db.update_document(COLLECTION, {"user_id": interaction.user.id}, {"coins": coins, "stocks": stocks})

        self.stop()
        lines = [f"Removed: {', '.join(sorted(coins_out | stocks_out))}"]
        if coins or stocks:
            lines.append(f"Crypto: {', '.join(coins) or 'none'}")
            lines.append(f"Stocks: {', '.join(s['symbol'] for s in stocks) or 'none'}")
        else:
            lines.append("Your watchlist is now empty.")
        await interaction.response.edit_message(content="\n".join(lines), view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Nothing removed.", view=None)


# --- Cog ------------------------------------------------------------------

class Crypto(commands.Cog):
    """Per-user crypto and stock watchlists with a daily price DM."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api_key = os.getenv("CMC_API")
        self._session: Optional[aiohttp.ClientSession] = None
        self._notify_semaphore: Optional[asyncio.Semaphore] = None
        # yf.download keeps its results in module-level state, so two
        # overlapping downloads (e.g. /addstock during the daily send) can
        # clobber each other. Serialise all yfinance calls.
        self._yf_lock: Optional[asyncio.Lock] = None

    async def cog_load(self):
        if not self.api_key:
            print("CMC_API is not set - crypto commands will not be able to fetch prices")
        await db.ensure_index(COLLECTION, [("user_id", 1)])
        await db.ensure_index(COLLECTION, [("notify", 1), ("next_send_utc", 1)])
        self._session = aiohttp.ClientSession()
        self._notify_semaphore = asyncio.Semaphore(NOTIFY_CONCURRENCY)
        self._yf_lock = asyncio.Lock()
        self.send_daily_prices.start()

    async def cog_unload(self):
        self.send_daily_prices.cancel()
        if self._session is not None:
            await self._session.close()

    # ------------------------------------------------------------- CMC API

    async def _cmc_get(self, url: str, params: dict) -> Tuple[int, dict]:
        if not self.api_key:
            raise CoinMarketCapError("CMC_API is not set")
        headers = {"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"}
        try:
            async with self._session.get(
                url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                return resp.status, await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise CoinMarketCapError(f"request failed: {e}") from e

    async def fetch_quotes(self, symbols: List[str]) -> Dict[str, dict]:
        """
        Look up the latest USD quotes for `symbols`. Returns {SYMBOL: coin}
        for every symbol CoinMarketCap recognises; unknown symbols are just
        absent. Raises CoinMarketCapError if the request itself fails.
        """
        if not symbols:
            return {}

        status_code, payload = await self._cmc_get(
            CMC_QUOTES_URL, {"symbol": ",".join(symbols), "convert": "USD", "skip_invalid": "true"}
        )
        status = payload.get("status", {})
        if status_code != 200:
            # When every requested symbol is invalid CMC answers 400 rather
            # than an empty result, so treat that as "nothing found".
            if status_code == 400 and "symbol" in (status.get("error_message") or "").lower():
                return {}
            raise CoinMarketCapError(f"HTTP {status_code}: {status.get('error_message')}")

        quotes = {}
        for symbol, matches in (payload.get("data") or {}).items():
            if not matches:
                continue
            # Several coins can share a ticker; take the highest ranked one.
            best = min(matches, key=lambda c: (c.get("cmc_rank") is None, c.get("cmc_rank") or 0))
            quotes[symbol.upper()] = best
        return quotes

    async def fetch_sentiment(self) -> Optional[dict]:
        """The crypto market's Fear & Greed reading as {"value", "label"}, or None if unavailable.
        Purely decorative, so failures are logged rather than raised."""
        try:
            status_code, payload = await self._cmc_get(CMC_FEAR_GREED_URL, {})
            if status_code != 200:
                raise CoinMarketCapError(f"HTTP {status_code}: {payload.get('status', {}).get('error_message')}")
            data = payload["data"]
            return {"value": int(data["value"]), "label": data["value_classification"]}
        except (CoinMarketCapError, KeyError, TypeError, ValueError) as e:
            print(f"Fear & Greed fetch failed: {e}")
            return None

    # -------------------------------------------------------------- yfinance

    async def fetch_stock_quotes(self, symbols: List[str]) -> Dict[str, dict]:
        """Like fetch_quotes, for stocks: unknown tickers are absent; raises StockDataError on failure."""
        if not symbols:
            return {}
        async with self._yf_lock:
            try:
                return await asyncio.to_thread(_download_stock_quotes, symbols)
            except Exception as e:
                raise StockDataError(str(e)) from e

    async def fetch_stock_meta(self, symbols: List[str]) -> List[dict]:
        async with self._yf_lock:
            return await asyncio.gather(*(asyncio.to_thread(_stock_meta, s) for s in symbols))

    async def fetch_market_data(self, coins: List[str], stocks: List[str]):
        """
        Everything a daily message needs, fetched concurrently:
        (quotes, sentiment, stock_quotes). quotes / stock_quotes is None if
        that source failed; sentiment is None if unavailable or there are no coins.
        """
        async def coin_quotes():
            try:
                return await self.fetch_quotes(coins)
            except CoinMarketCapError as e:
                print(f"CoinMarketCap quotes fetch failed: {e}")
                return None

        async def stock_quotes():
            try:
                return await self.fetch_stock_quotes(stocks)
            except StockDataError as e:
                print(f"Yahoo Finance fetch failed: {e}")
                return None

        async def sentiment():
            return await self.fetch_sentiment() if coins else None

        return await asyncio.gather(coin_quotes(), sentiment(), stock_quotes())

    # --------------------------------------------------------------- helpers

    async def _utc_offset(self, user_id: int) -> float:
        settings = await db.find_one("user_settings", {"user_id": user_id})
        return settings["utc_offset"] if settings else 0

    async def _save_list(self, user_id: int, doc: Optional[dict], field: str, items: list) -> str:
        """
        Store `items` as the user's `field` ("coins" or "stocks"), creating
        their watchlist - with daily DMs on - if this is their first add.
        Returns a note about the DM setting for the command's reply.
        """
        if doc is None:
            new_doc = {
                "user_id": user_id,
                "coins": [],
                "stocks": [],
                "notify": True,
                "next_send_utc": next_daily_send(await self._utc_offset(user_id)),
            }
            new_doc[field] = items
            await db.insert_document(COLLECTION, new_doc)
            return f"Daily price DMs are **on** ({DAILY_SEND_HOUR}:00 your time) - use /togglenotif to turn them off."

        await db.update_document(COLLECTION, {"user_id": user_id}, {field: items})
        if not doc.get("notify"):
            return "Daily price DMs are currently **off** - use /togglenotif to turn them on."
        return ""

    # ------------------------------------------------------------ commands

    @app_commands.command(name="addcoin", description="Add cryptocurrencies to your daily price watchlist")
    @app_commands.describe(tickers="One or more ticker symbols separated by commas, e.g. BTC, ETH, SOL")
    async def addcoin(self, interaction: discord.Interaction, tickers: str):
        symbols = parse_symbols(tickers)
        if not symbols:
            await interaction.response.send_message("Give at least one ticker symbol, e.g. BTC or BTC, ETH.", ephemeral=True)
            return

        doc = await db.find_one(COLLECTION, {"user_id": interaction.user.id})
        coins = doc.get("coins", []) if doc else []

        malformed = [s for s in symbols if not COIN_SYMBOL_RE.match(s)]
        already = [s for s in symbols if s in coins]
        to_check = [s for s in symbols if s not in malformed and s not in already]

        room = MAX_COINS - len(coins)
        if to_check and room <= 0:
            await interaction.response.send_message(
                f"You can track at most {MAX_COINS} coins. Remove some with /editlist first.",
                ephemeral=True,
            )
            return

        # The API call can take a moment - defer so the interaction doesn't time out.
        await interaction.response.defer(ephemeral=True)

        added, unknown, no_room = [], [], []
        if to_check:
            try:
                # One request for the whole list (CMC charges per 100 symbols, not per symbol).
                quotes = await self.fetch_quotes(to_check)
            except CoinMarketCapError as e:
                print(f"CoinMarketCap lookup for {to_check} failed: {e}")
                await interaction.followup.send("Couldn't reach CoinMarketCap right now - try again later.", ephemeral=True)
                return

            for symbol in to_check:
                coin = quotes.get(symbol)
                if coin is None:
                    unknown.append(symbol)
                elif len(added) >= room:
                    no_room.append(symbol)
                else:
                    added.append((symbol, coin))

        notif_note = ""
        if added:
            notif_note = await self._save_list(
                interaction.user.id, doc, "coins", coins + [symbol for symbol, _ in added]
            )

        added_lines = [
            f"- **{symbol}** ({coin['name']}), currently {format_price(coin['quote']['USD']['price'])}"
            for symbol, coin in added
        ]
        await interaction.followup.send(
            add_report(added_lines, already, unknown, "CoinMarketCap", malformed, no_room, MAX_COINS, notif_note),
            ephemeral=True,
        )

    @app_commands.command(name="addstock", description="Add stocks to your daily price watchlist")
    @app_commands.describe(tickers="One or more Yahoo Finance tickers separated by commas, e.g. AAPL, MSFT, SHOP.TO")
    async def addstock(self, interaction: discord.Interaction, tickers: str):
        symbols = parse_symbols(tickers)
        if not symbols:
            await interaction.response.send_message("Give at least one ticker symbol, e.g. AAPL or AAPL, MSFT.", ephemeral=True)
            return

        doc = await db.find_one(COLLECTION, {"user_id": interaction.user.id})
        stocks = doc.get("stocks", []) if doc else []
        tracked = {s["symbol"] for s in stocks}

        malformed = [s for s in symbols if not STOCK_SYMBOL_RE.match(s)]
        already = [s for s in symbols if s in tracked]
        to_check = [s for s in symbols if s not in malformed and s not in already]

        room = MAX_STOCKS - len(stocks)
        if to_check and room <= 0:
            await interaction.response.send_message(
                f"You can track at most {MAX_STOCKS} stocks. Remove some with /editlist first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        added, unknown, no_room = [], [], []
        if to_check:
            try:
                quotes = await self.fetch_stock_quotes(to_check)
            except StockDataError as e:
                print(f"Yahoo Finance lookup for {to_check} failed: {e}")
                await interaction.followup.send("Couldn't reach Yahoo Finance right now - try again later.", ephemeral=True)
                return

            valid = []
            for symbol in to_check:
                if symbol not in quotes:
                    unknown.append(symbol)
                elif len(valid) >= room:
                    no_room.append(symbol)
                else:
                    valid.append(symbol)
            added = [(meta, quotes[meta["symbol"]]) for meta in await self.fetch_stock_meta(valid)]

        notif_note = ""
        if added:
            notif_note = await self._save_list(
                interaction.user.id, doc, "stocks", stocks + [meta for meta, _ in added]
            )

        added_lines = [
            f"- **{meta['symbol']}** ({meta['name']}), currently {format_money(quote['price'], meta['currency'])}"
            for meta, quote in added
        ]
        await interaction.followup.send(
            add_report(added_lines, already, unknown, "Yahoo Finance", malformed, no_room, MAX_STOCKS, notif_note),
            ephemeral=True,
        )

    @app_commands.command(name="editlist", description="Remove coins or stocks from your watchlist")
    async def editlist(self, interaction: discord.Interaction):
        doc = await db.find_one(COLLECTION, {"user_id": interaction.user.id}) or {}
        coins, stocks = doc.get("coins", []), doc.get("stocks", [])

        if not coins and not stocks:
            await interaction.response.send_message(
                "Your watchlist is empty - add some with /addcoin or /addstock.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Tick what you want to remove, then press **Remove selected**:",
            view=EditListView(coins, stocks),
            ephemeral=True,
        )

    @app_commands.command(name="showlist", description="DM yourself your watchlist's current prices, like the daily message")
    async def showlist(self, interaction: discord.Interaction):
        doc = await db.find_one(COLLECTION, {"user_id": interaction.user.id}) or {}
        coins, stocks = doc.get("coins", []), doc.get("stocks", [])

        if not coins and not stocks:
            await interaction.response.send_message(
                "Your watchlist is empty - add some with /addcoin or /addstock.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        quotes, sentiment, stock_quotes = await self.fetch_market_data(coins, [s["symbol"] for s in stocks])
        embeds = daily_embeds(coins, quotes or {}, sentiment, stocks, stock_quotes or {})

        try:
            await interaction.user.send(embeds=embeds)
        except discord.Forbidden:
            await interaction.followup.send(
                "I couldn't DM you - allow direct messages from server members in your privacy settings and try again.",
                ephemeral=True,
            )
            return

        text = "Sent your watchlist prices to your DMs."
        failed = [name for name, data, wanted in (
            ("CoinMarketCap", quotes, coins), ("Yahoo Finance", stock_quotes, stocks),
        ) if wanted and data is None]
        if failed:
            text += f"\nCouldn't reach {' or '.join(failed)}, so some prices are missing."
        await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(name="togglenotif", description="Turn your daily crypto and stock price DMs on or off")
    async def togglenotif(self, interaction: discord.Interaction):
        doc = await db.find_one(COLLECTION, {"user_id": interaction.user.id})
        enable = not (doc or {}).get("notify", False)

        update = {"user_id": interaction.user.id, "notify": enable}
        if enable:
            update["next_send_utc"] = next_daily_send(await self._utc_offset(interaction.user.id))
        if doc is None:
            update["coins"] = []
            update["stocks"] = []
        await db.upsert_document(COLLECTION, {"user_id": interaction.user.id}, update)

        if not enable:
            await interaction.response.send_message("Daily price DMs turned **off**.", ephemeral=True)
            return

        ts = int(update["next_send_utc"].timestamp())
        text = f"Daily price DMs turned **on**. Next one: <t:{ts}:F>."
        if not (doc or {}).get("coins") and not (doc or {}).get("stocks"):
            text += "\nYour watchlist is empty - add some with /addcoin or /addstock."
        await interaction.response.send_message(text, ephemeral=True)

    # ------------------------------------------------------ background loop

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def send_daily_prices(self):
        # See calendar.py: DMs aren't shard-specific, so only one shard sends.
        if getattr(self.bot, "shard_id", None) not in (None, 0):
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
        # One request per data source for every distinct ticker in the batch,
        # rather than one per user - keeps us well inside CoinMarketCap's
        # credit limits and avoids hammering Yahoo.
        coins = sorted({symbol for doc in batch for symbol in doc.get("coins", [])})
        stocks = sorted({s["symbol"] for doc in batch for s in doc.get("stocks", [])})
        quotes, sentiment, stock_quotes = await self.fetch_market_data(coins, stocks)

        if coins and quotes is None and self.api_key:
            # Leave next_send_utc alone so this batch is retried next tick.
            # (A Yahoo failure doesn't hold things up - those users just get
            # "No data available" for their stocks.)
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
            *(
                self._notify_bounded(
                    doc["user_id"],
                    daily_embeds(doc.get("coins", []), quotes or {}, sentiment, doc.get("stocks", []), stock_quotes or {}),
                )
                for doc in batch
                if doc.get("coins") or doc.get("stocks")
            ),
            return_exceptions=True,
        )

    async def _notify_bounded(self, user_id: int, embeds: List[discord.Embed]):
        async with self._notify_semaphore:
            await self._notify(user_id, embeds)

    async def _notify(self, user_id: int, embeds: List[discord.Embed]):
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
        except discord.NotFound:
            return

        try:
            await user.send(embeds=embeds)
        except discord.Forbidden:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Crypto(bot))
