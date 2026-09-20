import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TEST_GUILD = discord.Object(id=int(os.getenv('TEST_GUILD_ID')))

#Boilerplate code 
class Client(commands.Bot):
    async def on_ready(self):
        print(f'Connected as {self.user}')
        # Copy the global commands into your test server so they show up
        # instantly, instead of waiting up to an hour for the global sync.
        self.tree.copy_global_to(guild=TEST_GUILD)
        synced = await self.tree.sync(guild=TEST_GUILD)
        print(f'Synced {len(synced)} command(s) to test server')

intents = discord.Intents.default()
intents.message_content = True
client = Client(command_prefix="!", intents=intents) #Does nothing but is needed to run the rest of the code

#basic function
@client.tree.command(name="hello", description="Say hello back", guild= TEST_GUILD)
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(f'Hi there {interaction.user.mention}')

#Basic sending function
@client.tree.command(name="printer", description="Print the message provided", guild= TEST_GUILD)
async def printer(interaction: discord.Interaction, print: str):
    await interaction.response.send_message(print)

#Basic button function
class View (discord.ui.View):
    @discord.ui.button(label="Click me", style=discord.ButtonStyle.red, emoji="🙁")
    async def button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Hi")

@client.tree.command(name="button", description="Generate a button", guild= TEST_GUILD)
async def myButton(interaction: discord.Interaction):
    await interaction.response.send_message("Click me for virus", view = View())

#Basic embed function
@client.tree.command(name="embed", description="Generate an embed", guild= TEST_GUILD)
async def embed(interaction: discord.Interaction):
    embed = discord.Embed(title ="i am a title",url = "https://www.youtube.com/watch?v=KHQ2MaDbx5I&t=317s", description = "Description")
    embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/842055872778993679/1548686866054647888/IMG_0431.png?ex=6aabeb0a&is=6aaa998a&hm=5d8d8c9c4f275bfcc9bf70598d4ed7269e5734466fae1afd42fda2d66949f92e&")
    embed.add_field(name = "Field 1 Title",value = "First value", inline = True)
    embed.add_field(name = "Field 2 Title",value = "second value", inline = True)
    await interaction.response.send_message(embed = embed)


#Basic menu function
class Menu(discord.ui.Select):
    def __init__(self):
        options =[
            discord.SelectOption(
                label = "Gamer",
                description= "Gaming",
                emoji="😭"
            ),

            discord.SelectOption(
                label = "Gamer 2",
                description= "Gaming++",
                emoji="😭"
            )
        ]

        super().__init__(placeholder = "Please select an option", min_values = 1, max_values = 1, options = options)
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "Gamer":
            await interaction.response.send_message(f"You picked {self.values[0]}")
        elif self.values[0] == "Gamer 2":
            await interaction.response.send_message(f"You picked wrong")

class MenuView(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(Menu())

@client.tree.command(name="menu", description="Show a dropdown menu", guild= TEST_GUILD)
async def myMenu(interaction: discord.Interaction):
    await interaction.response.send_message(view= MenuView())


client.run(os.getenv('DISCORD_TOKEN'))
