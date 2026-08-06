import discord
from discord.ext import commands, tasks
from discord import app_commands
import database as db

class LoadoutModal(discord.ui.Modal, title='Proponi il tuo Meta Loadout'):
    weapon_name = discord.ui.TextInput(
        label='Nome Arma',
        placeholder='Es. Superi 46, Kar98k, MCW',
        required=True,
        max_length=50
    )
    category = discord.ui.TextInput(
        label='Categoria',
        placeholder='Es. Assalto, Mitraglietta, Cecchino',
        required=True,
        max_length=30
    )
    attachments = discord.ui.TextInput(
        label='Accessori (Separati da virgola)',
        style=discord.TextStyle.paragraph,
        placeholder='Volata: X, Canna: Y, Ottica: Z, Munizioni: A, Calcio: B',
        required=True,
        max_length=300
    )

    async def on_submit(self, interaction: discord.Interaction):
        db.add_loadout(
            self.weapon_name.value,
            self.category.value,
            self.attachments.value,
            str(interaction.user.id),
            interaction.user.display_name
        )
        await interaction.response.send_message(
            f"✅ Loadout per **{self.weapon_name.value}** salvato con successo nell'armeria globale!\nTutti potranno vederlo sulla Webapp.",
            ephemeral=True
        )


class LoadoutsPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Visualizza i migliori Loadout", style=discord.ButtonStyle.primary, custom_id="btn_view_loadouts", emoji="🔫")
    async def btn_view(self, interaction: discord.Interaction, button: discord.ui.Button):
        loadouts = db.get_all_loadouts()
        if not loadouts:
            await interaction.response.send_message("L'armeria è vuota! Sii il primo a proporre un loadout.", ephemeral=True)
            return
            
        top_loadouts = loadouts[:3]
        embed = discord.Embed(
            title="🏆 I Top 3 Loadout della Community",
            description="Ecco i loadout più votati/recenti condivisi dai giocatori di FraDodo!",
            color=discord.Color.red()
        )
        for l in top_loadouts:
            embed.add_field(
                name=f"{l['weapon_name']} ({l['category']})",
                value=f"**Accessori:**\n{l['attachments']}\n\n*Proposto da: {l['author_name']} | Voti: {l['votes']}*",
                inline=False
            )
            
        # Tasto per andare alla Webapp
        with open("current_url.txt", "r") as f:
            url = f.read().strip()
        
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Apri Armeria Completa (Web)", url=f"{url}/loadouts", style=discord.ButtonStyle.link, emoji="🌐"))
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Proponi un Loadout", style=discord.ButtonStyle.success, custom_id="btn_submit_loadout", emoji="➕")
    async def btn_submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LoadoutModal())


class Loadouts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(LoadoutsPanelView())
        self.meta_decay_task.start()

    def cog_unload(self):
        self.meta_decay_task.cancel()

    @tasks.loop(hours=24)
    async def meta_decay_task(self):
        # Ogni 24 ore decresce i voti
        db.decay_loadout_votes()
        print("[Loadouts] Eseguito il decay dei voti dei meta loadouts.")

    @meta_decay_task.before_loop
    async def before_meta_decay(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="setup_loadouts", description="Piazza il pannello dell'Armeria Meta in questo canale")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_loadouts(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🔫 Armeria: Meta Loadouts",
            description="Benvenuti nell'Armeria della community!\n\nQui potete condividere le vostre armi migliori e visualizzare i loadout più forti (Meta) del momento.\n\nUsa i pulsanti qui sotto per interagire.",
            color=discord.Color.dark_theme()
        )
        embed.set_image(url="https://www.callofduty.com/content/dam/atvi/callofduty/cod-touchui/mw3/meta-images/S4-RELOADED-MW3.jpg")
        
        await interaction.channel.send(embed=embed, view=LoadoutsPanelView())
        await interaction.response.send_message("Pannello Loadouts creato con successo!", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Loadouts(bot))
