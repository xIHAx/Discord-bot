import os
import discord
from discord import app_commands
from discord.ext import commands

# --- UI components (views / selects) live next to the commands that use them ---

class ButtonView(discord.ui.View):
    @discord.ui.button(label="Click me", style=discord.ButtonStyle.red, emoji="🙁")
    async def button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Hi")


class OptionsMenu(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Gamer", description="Gaming", emoji="😭"),
            discord.SelectOption(label="Gamer 2", description="Gaming++", emoji="😭"),
        ]
        super().__init__(placeholder="Please select an option", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "Gamer":
            await interaction.response.send_message(f"You picked {self.values[0]}")
        elif self.values[0] == "Gamer 2":
            await interaction.response.send_message("You picked wrong")


class MenuView(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(OptionsMenu())


# --- Commands that use them ---

class UI(commands.Cog):
    """Commands that demonstrate buttons, embeds and select menus."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="button", description="Generate a button")
    async def button(self, interaction: discord.Interaction):
        await interaction.response.send_message("Click me for virus", view=ButtonView())

    @app_commands.command(name="embed", description="Generate an embed")
    async def embed(self, interaction: discord.Interaction):
        e = discord.Embed(
            title="i am a title",
            url="https://www.youtube.com/watch?v=KHQ2MaDbx5I&t=317s",
            description="Description",
        )
        e.set_thumbnail(
            url="https://cdn.discordapp.com/attachments/842055872778993679/1548686866054647888/IMG_0431.png?ex=6aabeb0a&is=6aaa998a&hm=5d8d8c9c4f275bfcc9bf70598d4ed7269e5734466fae1afd42fda2d66949f92e&"
        )
        e.add_field(name="Field 1 Title", value="First value", inline=True)
        e.add_field(name="Field 2 Title", value="second value", inline=True)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="menu", description="Show a dropdown menu")
    async def menu(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=MenuView())


async def setup(bot: commands.Bot):
    await bot.add_cog(UI(bot))
