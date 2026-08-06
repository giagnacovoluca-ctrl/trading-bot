import discord
from discord.ext import commands
from discord import app_commands
import random

URZIKSTAN_POIS = [
    "Levin Resort", "Popov Power", "Orlov Military Base", "Seaport District",
    "Urzikstan Cargo", "Old Town", "Low Town", "Hadiqa Farms",
    "Zaravan City", "Zaravan Suburbs", "Shahin Manor"
]

REBIRTH_POIS = [
    "Bioweapons", "Industry", "Chemical Engineering", "Harbor",
    "Prison", "Headquarters", "Factory", "Control Center",
    "Living Quarters", "Stronghold", "Dock"
]

class DropView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Urzikstan", style=discord.ButtonStyle.success, custom_id="btn_drop_urzikstan", emoji="🌍")
    async def btn_urzikstan(self, interaction: discord.Interaction, button: discord.ui.Button):
        poi = random.choice(URZIKSTAN_POIS)
        embed = discord.Embed(
            title="🪂 Lancio su Urzikstan!",
            description=f"{interaction.user.mention} ha fatto girare la ruota...\n\nDestinazione: **{poi}**!\nNiente lamentele, andate e dominate.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)

    @discord.ui.button(label="Rebirth Island", style=discord.ButtonStyle.blurple, custom_id="btn_drop_rebirth", emoji="🏝️")
    async def btn_rebirth(self, interaction: discord.Interaction, button: discord.ui.Button):
        poi = random.choice(REBIRTH_POIS)
        embed = discord.Embed(
            title="🪂 Lancio su Rebirth Island!",
            description=f"{interaction.user.mention} ha fatto girare la ruota...\n\nDestinazione: **{poi}**!\nNiente lamentele, andate e dominate.",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed)


class Fun(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(DropView()) # Rende i bottoni persistenti ai riavvii

    @app_commands.command(name="setup_drop", description="Piazza il pannello con i bottoni per il Drop Casuale in questo canale")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_drop(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎯 Generatore di Drop",
            description="Non sapete dove buttarvi? Siete stufi di litigare su chi deve scegliere la zona di atterraggio?\n\nCliccate su uno dei bottoni qui sotto e il bot sceglierà casualmente e imparzialmente per voi!",
            color=discord.Color.gold()
        )
        await interaction.channel.send(embed=embed, view=DropView())
        await interaction.response.send_message("Pannello creato con successo!", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Fun(bot))
