import discord
from discord import app_commands
from discord.ext import commands

# Discord caps any select menu at 25 options.
MAX_OPTIONS = 25

class VoiceChannelSelect(discord.ui.Select):
    def __init__(self, guild: discord.Guild):
        voice_channels = guild.voice_channels[:MAX_OPTIONS]

        if voice_channels:
            options = [
                discord.SelectOption(label=vc.name, value=str(vc.id))
                for vc in voice_channels
            ]
        else:
            # A Select can't be created with zero options, so fall back to
            # a disabled placeholder entry instead of crashing.
            options = [discord.SelectOption(label="No voice channels found", value="none")]

        super().__init__(
            placeholder="Select a voice channel",
            min_values=1,
            max_values=1,
            options=options,
            disabled=not voice_channels,
        )

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(int(self.values[0]))
        member_count = len(channel.members)
        await interaction.response.send_message(
            f"You picked **{channel.name}** ({member_count} member{'s' if member_count != 1 else ''} in it)",
            ephemeral=True,
        )


class VoiceChannelView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.add_item(VoiceChannelSelect(guild))


class Voice(commands.Cog):
    """Commands related to voice channels."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="voicechannels", description="Show a dropdown of this server's voice channels")
    async def voicechannels(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Pick a voice channel:",
            view=VoiceChannelView(interaction.guild),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Voice(bot))
