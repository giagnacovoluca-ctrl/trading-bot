import discord
from discord.ext import commands
from discord import app_commands

class LFGJoinView(discord.ui.View):
    def __init__(self, creator_id: int, missing_players: int):
        super().__init__(timeout=None)
        self.creator_id = creator_id
        self.missing_players = missing_players
        self.joined_players = []

    @discord.ui.button(label="Unisciti", style=discord.ButtonStyle.success, emoji="🎮", custom_id="lfg_join_btn")
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == self.creator_id:
            await interaction.response.send_message("Sei il creatore di questo gruppo!", ephemeral=True)
            return
            
        if interaction.user.mention in self.joined_players:
            await interaction.response.send_message("Sei già nel gruppo!", ephemeral=True)
            return
            
        if len(self.joined_players) >= self.missing_players:
            await interaction.response.send_message("Il gruppo è già pieno!", ephemeral=True)
            return

        self.joined_players.append(interaction.user.mention)
        
        # Update embed
        embed = interaction.message.embeds[0]
        players_str = "\n".join(self.joined_players)
        
        # Find and update the "Giocatori Uniti" field
        for i, field in enumerate(embed.fields):
            if field.name.startswith("Giocatori Uniti"):
                embed.set_field_at(i, name=f"Giocatori Uniti ({len(self.joined_players)}/{self.missing_players})", value=players_str, inline=False)
                break
                
        if len(self.joined_players) >= self.missing_players:
            button.disabled = True
            button.label = "Pieno!"
            button.style = discord.ButtonStyle.secondary
            embed.color = discord.Color.red()
            
        await interaction.message.edit(embed=embed, view=self)
        await interaction.response.send_message(f"Ti sei unito al gruppo di <@{self.creator_id}>!", ephemeral=True)
        # Ping the creator in the channel
        await interaction.channel.send(f"🔔 <@{self.creator_id}>, {interaction.user.mention} si è unito al tuo team!", delete_after=60)

class LFGModal(discord.ui.Modal, title='Cerca Squadra (LFG)'):
    modalita = discord.ui.TextInput(
        label='Modalità',
        placeholder='Es. Ritorno, Ranked, Battle Royale...',
        required=True,
    )
    mancanti = discord.ui.TextInput(
        label='Giocatori Mancanti',
        placeholder='1, 2 o 3',
        required=True,
        max_length=1
    )
    requisiti = discord.ui.TextInput(
        label='Requisiti (KD, Microfono, ecc.)',
        style=discord.TextStyle.paragraph,
        placeholder='Es. Microfono obbligatorio, KD > 1.5, chill',
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            mancanti_int = int(self.mancanti.value)
            if not 1 <= mancanti_int <= 3:
                raise ValueError
        except:
            await interaction.response.send_message("Errore: I giocatori mancanti devono essere un numero da 1 a 3.", ephemeral=True)
            return

        req = self.requisiti.value if self.requisiti.value else "Nessuno (Chill)"
        
        embed = discord.Embed(
            title=f"🔎 Cerca Squadra: {self.modalita.value}",
            description=f"{interaction.user.mention} sta cercando compagni per formare un team!",
            color=discord.Color.green()
        )
        embed.add_field(name="Requisiti", value=req, inline=False)
        embed.add_field(name=f"Giocatori Uniti (0/{mancanti_int})", value="Nessuno per ora...", inline=False)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        
        view = LFGJoinView(interaction.user.id, mancanti_int)
        
        await interaction.response.send_message(f"Annuncio creato da {interaction.user.mention}!", embed=embed, view=view)


class LFGPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Crea Annuncio LFG", style=discord.ButtonStyle.primary, custom_id="lfg_create_btn", emoji="🔎")
    async def create_lfg(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LFGModal())


class LFG(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(LFGPanelView())
        # We ideally should also re-add the join views, but without persistence database, 
        # the join buttons will fail if the bot restarts. 
        # Since this is a temporary queue, it's generally acceptable.

    @app_commands.command(name="setup_lfg", description="Piazza il pannello LFG (Cerca Squadra) in questo canale")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_lfg(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🤝 Cerca Squadra (LFG)",
            description="Vuoi giocare a Warzone ma ti mancano dei compagni?\n\nClicca sul bottone qui sotto per creare un **Annuncio**. Specifica a cosa vuoi giocare e quali sono i requisiti (se ne hai). Gli altri potranno unirsi al tuo team con un semplice click!",
            color=discord.Color.blue()
        )
        embed.set_image(url="https://www.callofduty.com/content/dam/atvi/callofduty/cod-touchui/mw2/meta-images/WZ2-META.jpg")
        
        await interaction.channel.send(embed=embed, view=LFGPanelView())
        await interaction.response.send_message("Pannello LFG creato con successo!", ephemeral=True)

async def setup(bot):
    await bot.add_cog(LFG(bot))
