import asyncio
import os
import signal

import discord
from aiohttp import web
from discord.ext import commands
from dotenv import load_dotenv

import db

# Locally this reads .env; on Cloud Run there's no .env and the values come
# from the service's environment variables / secrets instead.
load_dotenv()

TEST_GUILD = discord.Object(id=int(os.getenv('TEST_GUILD_ID')))

# Every cog module listed here gets loaded on startup.
# Add a new entry whenever you create a new file in cogs/.
INITIAL_EXTENSIONS = [
    "cogs.calendar",
    "cogs.crypto",
]


class Client(commands.Bot):
    _health_runner = None

    async def setup_hook(self):
        # Cloud Run sets PORT and won't route to (or keep) the container
        # unless something is listening on it, so start the health check
        # first. Not set when running locally, so nothing extra runs there.
        if os.getenv("PORT"):
            await self._start_health_server(int(os.getenv("PORT")))

        # Cloud Run stops containers with SIGTERM. The bot runs as PID 1
        # there, which ignores signals it has no handler for - so without
        # this it would hang until SIGKILL instead of disconnecting cleanly.
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(self.close()))
        except (NotImplementedError, RuntimeError):
            pass  # Windows doesn't support add_signal_handler; not needed locally.

        # Connect to MongoDB before anything else needs it.
        db.init_db()

        # setup_hook runs once, before on_ready, and is the recommended
        # place to load extensions (discord.py docs call this out
        # specifically).
        for extension in INITIAL_EXTENSIONS:
            await self.load_extension(extension)

    async def _start_health_server(self, port: int):
        async def health(request: web.Request) -> web.Response:
            return web.Response(text="ok" if self.is_ready() else "starting")

        app = web.Application()
        app.router.add_get("/", health)
        self._health_runner = web.AppRunner(app)
        await self._health_runner.setup()
        await web.TCPSite(self._health_runner, "0.0.0.0", port).start()
        print(f"Health check listening on port {port}")

    async def close(self):
        if self._health_runner is not None:
            await self._health_runner.cleanup()
        db.close_db()
        await super().close()

    async def on_ready(self):
        print(f'Connected as {self.user}')
        # Copy the global commands into your test server so they show up
        # instantly, instead of waiting up to an hour for the global sync.
        self.tree.copy_global_to(guild=TEST_GUILD)
        synced = await self.tree.sync(guild=TEST_GUILD)
        print(f'Synced {len(synced)} command(s) to test server')


intents = discord.Intents.default()
intents.message_content = True
client = Client(command_prefix="!", intents=intents)  # Does nothing but is needed to run the rest of the code

client.run(os.getenv('DISCORD_TOKEN'))
