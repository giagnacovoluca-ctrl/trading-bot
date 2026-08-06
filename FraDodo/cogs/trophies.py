import discord
from discord.ext import commands
from discord import app_commands
import database as db

class Trophies(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="trofei", description="Mostra la Hall of Fame (Statistiche Avanzate e Record)")
    async def trofei(self, interaction: discord.Interaction):
        hof = db.get_hall_of_fame()
        
        if not hof:
            await interaction.response.send_message("Nessun record disponibile. Inizia a registrare le tue partite!", ephemeral=True)
            return

        embed = discord.Embed(
            title="🏆 Hall of Fame - I Migliori di FraDodo",
            description="Ecco i record assoluti registrati dalla community in questa stagione!",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url="https://i.imgur.com/B7y3L29.png") # Generic trophy icon
        
        if 'top_killer' in hof:
            embed.add_field(
                name="💀 Mietitore (Record Kills)",
                value=f"**{hof['top_killer']['name']}** con **{hof['top_killer']['value']}** Kills in una singola partita!",
                inline=False
            )
            
        if 'top_damage' in hof:
            embed.add_field(
                name="💥 Demolitore (Record Danni)",
                value=f"**{hof['top_damage']['name']}** con **{hof['top_damage']['value']}** Danni in una singola partita!",
                inline=False
            )
            
        if 'top_score' in hof:
            embed.add_field(
                name="👑 Campione (Record Vittorie)",
                value=f"**{hof['top_score']['name']}** con un totale di **{hof['top_score']['value']}** vittorie!",
                inline=False
            )
            
        if 'most_dedicated' in hof:
            embed.add_field(
                name="🏅 Veterano (Più Partite)",
                value=f"**{hof['most_dedicated']['name']}** con ben **{hof['most_dedicated']['value']}** partite giocate e approvate!",
                inline=False
            )

        with open("current_url.txt", "r") as f:
            url = f.read().strip()
            
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Vedi Trofei Completi sul Web", url=f"{url}/trofei", style=discord.ButtonStyle.link, emoji="🌐"))

        await interaction.response.send_message(embed=embed, view=view)

async def setup(bot):
    await bot.add_cog(Trophies(bot))
