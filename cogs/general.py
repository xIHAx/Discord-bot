import os
import discord
from discord import app_commands
from discord.ext import commands

class General(commands.Cog):
    """Basic, no-frills slash commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="hello", description="Say hello back")
    async def hello(self, interaction: discord.Interaction):
        await interaction.response.send_message(f'Hi there {interaction.user.mention}')

    @app_commands.command(name="printer", description="Print the message provided")
    async def printer(self, interaction: discord.Interaction, print: str):
        await interaction.response.send_message(print)


# Every cog file needs an async setup() function - this is the hook
# bot.load_extension() calls to register the cog with the bot.
async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))
