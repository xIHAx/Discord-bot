import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

import db

load_dotenv()

TEST_GUILD = discord.Object(id=int(os.getenv('TEST_GUILD_ID')))

# Every cog module listed here gets loaded on startup.
# Add a new entry whenever you create a new file in cogs/.
INITIAL_EXTENSIONS = [
    "cogs.general",
    "cogs.ui",
    "cogs.voice",
    "cogs.data",
    "cogs.calendar",
]


class Client(commands.Bot):
    async def setup_hook(self):
        # Connect to MongoDB before anything else needs it.
        db.init_db()

        # setup_hook runs once, before on_ready, and is the recommended
        # place to load extensions (discord.py docs call this out
        # specifically).
        for extension in INITIAL_EXTENSIONS:
            await self.load_extension(extension)

    async def close(self):
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
