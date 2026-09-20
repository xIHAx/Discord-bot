from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import db


class Data(commands.Cog):
    """Commands that read/write JSON documents in MongoDB (not files)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="note", description="Save a note to the database")
    @app_commands.describe(title="A short title for the note", content="The note's content")
    async def note(self, interaction: discord.Interaction, title: str, content: str):
        # Everything here becomes one JSON document in the "notes" collection.
        document = {
            "user_id": interaction.user.id,
            "username": str(interaction.user),
            "title": title,
            "content": content,
            "created_at": datetime.now(timezone.utc),
        }

        note_id = await db.insert_document("notes", document)

        await interaction.response.send_message(
            f"Saved **{title}** (id `{note_id}`)",
            ephemeral=True,
        )

    @app_commands.command(name="mynotes", description="List your saved notes")
    async def mynotes(self, interaction: discord.Interaction):
        notes = await db.find_documents("notes", {"user_id": interaction.user.id})

        if not notes:
            await interaction.response.send_message("You don't have any notes saved.", ephemeral=True)
            return

        lines = [f"- **{n['title']}**: {n['content']}" for n in notes]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Data(bot))
