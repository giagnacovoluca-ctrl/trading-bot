import discord
from discord.ext import commands, tasks
from discord import app_commands
import database
from cod_api import get_latest_match

class MenuClassifica(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
        url = "https://dodo-cod-dashboard.loca.lt/"
        try:
            with open("current_url.txt", "r") as f:
                content = f.read().strip()
                if content:
                    url = content
        except Exception:
            pass
            
        self.add_item(discord.ui.Button(label="🌐 Apri Dashboard", url=url))
        
    @discord.ui.button(label="🏆 Classifica Top 10", style=discord.ButtonStyle.primary, custom_id="btn_classifica")
    async def btn_classifica(self, interaction: discord.Interaction, button: discord.ui.Button):
        players = database.get_all_players()
        if not players:
            await interaction.response.send_message("Non ci sono ancora giocatori iscritti!", ephemeral=True)
            return
            
        embed = discord.Embed(title="🏆 Classifica Globale Top 10", color=discord.Color.gold())
        for i, p in enumerate(players[:10]):
            totale_punti = p.get('points', 0) + p.get('contest_points', 0.0)
            embed.add_field(name=f"#{i+1} {p['activision_id']}", value=f"Punti: {totale_punti} | Match: {p['matches_played']}", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🎮 Le Mie Statistiche", style=discord.ButtonStyle.success, custom_id="btn_statistiche")
    async def btn_statistiche(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = database.get_player(str(interaction.user.id))
        if not player:
            await interaction.response.send_message("❌ Non sei iscritto. Usa `/iscriviti` per cominciare!", ephemeral=True)
            return
            
        totale_punti = player.get('points', 0) + player.get('contest_points', 0.0)
        embed = discord.Embed(title=f"📊 Statistiche di {player['activision_id']}", color=discord.Color.green())
        embed.add_field(name="Punti Totali", value=f"**{totale_punti}**", inline=True)
        embed.add_field(name="Match Giocati", value=f"{player['matches_played']}", inline=True)
        embed.add_field(name="Vittorie", value=f"{player['wins']}", inline=True)
        embed.add_field(name="Kills Totali", value=f"{player['total_kills']}", inline=True)
        embed.add_field(name="Danni Totali", value=f"{player['total_damage']}", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class Tournament(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.auto_track_matches.start()

    def cog_unload(self):
        self.auto_track_matches.cancel()

    @tasks.loop(minutes=15)
    async def auto_track_matches(self):
        """Loop in background per controllare in automatico le API di tutti i giocatori."""
        players = database.get_all_players()
        for player in players:
            try:
                activision_id = player['activision_id']
                discord_id = player['discord_id']
                match_data = await get_latest_match(activision_id)
                
                if match_data and match_data.get("match_id"):
                    # Verifica se la partita esiste già
                    if not database.match_exists(match_data["match_id"]):
                        # Salva la nuova partita e aggiorna i punteggi
                        database.save_match(
                            discord_id=discord_id,
                            kills=match_data.get("kills", 0),
                            damage=match_data.get("damage", 0),
                            placement=match_data.get("placement", 0),
                            status="approved",
                            api_match_id=match_data["match_id"]
                        )
                        print(f"Tracking automatico: Trovata nuova partita per {activision_id}!")
            except Exception as e:
                print(f"Errore durante l'auto-tracking per {player['activision_id']}: {e}")

    @auto_track_matches.before_loop
    async def before_auto_track(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="iscriviti", description="Registrati al torneo associando il tuo Activision ID")
    async def iscriviti(self, interaction: discord.Interaction, activision_id: str):
        discord_id = str(interaction.user.id)
        database.register_player(discord_id, activision_id)
        await interaction.response.send_message(f"✅ Iscrizione completata! Activision ID salvato: **{activision_id}**")

    @app_commands.command(name="verifica_match", description="Tenta di recuperare la tua ultima partita in automatico tramite API")
    async def verifica_match(self, interaction: discord.Interaction):
        discord_id = str(interaction.user.id)
        player = database.get_player(discord_id)
        
        if not player:
            await interaction.response.send_message("❌ Non sei ancora iscritto. Usa prima il comando `/iscriviti`.", ephemeral=True)
            return

        activision_id = player['activision_id']
        await interaction.response.defer()

        try:
            match_data = await get_latest_match(activision_id)
            if match_data:
                api_match_id = match_data.get("match_id")
                if api_match_id and database.match_exists(api_match_id):
                    await interaction.followup.send("⚠️ Questa partita è già stata registrata in precedenza!")
                    return
                    
                database.save_match(
                    discord_id=discord_id,
                    kills=match_data.get("kills", 0),
                    damage=match_data.get("damage", 0),
                    placement=match_data.get("placement", 0),
                    status="approved",
                    api_match_id=api_match_id
                )
                await interaction.followup.send(f"✅ Partita registrata con successo tramite API!\n"
                                                f"🔫 Kills: {match_data.get('kills')}\n"
                                                f"💥 Danni: {match_data.get('damage')}\n"
                                                f"🏆 Piazzamento: {match_data.get('placement')}")
            else:
                await interaction.followup.send("⚠️ Nessuna partita trovata di recente o token API scaduto.")
        except Exception as e:
            msg = (f"⚠️ Le API di Call of Duty sono attualmente instabili o in timeout.\n"
                   f"Usa il sistema di fallback con il comando `/carica_screenshot` per inviare l'immagine del tabellone.")
            await interaction.followup.send(msg)

    @app_commands.command(name="carica_contest", description="Carica foto e statistiche per il Contest Mensile")
    async def carica_contest(self, interaction: discord.Interaction, immagine: discord.Attachment, kills: int, posizione: int):
        discord_id = str(interaction.user.id)
        player = database.get_player(discord_id)
        if not player:
            await interaction.response.send_message("❌ Iscriviti prima col comando `/iscriviti`.", ephemeral=True)
            return
            
        if not immagine.content_type or not immagine.content_type.startswith("image/"):
            await interaction.response.send_message("❌ Il file inviato non è un'immagine valida.", ephemeral=True)
            return
            
        punti = database.save_contest_match(discord_id, kills, posizione, immagine.url)
        
        await interaction.response.send_message(
            f"🎯 **Contest Registrato per {player['activision_id']}!**\n"
            f"🔫 Kills: {kills}\n"
            f"🏆 Posizione: {posizione}\n"
            f"🔥 **Punti Contest Calcolati:** `{punti:.1f}`\n"
            f"[Apri l'immagine di prova]({immagine.url})"
        )

    @app_commands.command(name="dashboard", description="Visualizza il link per accedere alla dashboard del torneo")
    async def dashboard(self, interaction: discord.Interaction):
        url = "https://dodo-cod-dashboard.loca.lt/"
        try:
            with open("current_url.txt", "r") as f:
                content = f.read().strip()
                if content:
                    url = content
        except Exception:
            pass
            
        view = discord.ui.View()
        button = discord.ui.Button(label="Apri Dashboard", url=url)
        view.add_item(button)
        await interaction.response.send_message("🌐 Clicca il pulsante qui sotto per aprire la dashboard web del torneo:", view=view)

    @app_commands.command(name="menu", description="Apri l'hub principale del torneo con i comandi rapidi")
    async def menu(self, interaction: discord.Interaction):
        view = MenuClassifica()
        embed = discord.Embed(title="🎮 FraDodo COD Tournament Hub", description="Usa i bottoni sottostanti per navigare nel sistema del torneo!", color=discord.Color.blue())
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="setup_server", description="Crea automaticamente le categorie e i canali del torneo")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_server(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("Questo comando può essere usato solo in un server.", ephemeral=True)
            return

        await interaction.response.defer()

        try:
            # La cancellazione automatica delle vecchie categorie è stata disabilitata per sicurezza.

            # Crea nuova categoria professionale
            category = await guild.create_category("⸻ 🏆 DODO TOURNAMENT ⸻")

            # Crea canali testuali esteticamente gradevoli
            await guild.create_text_channel("📢・annunci-ufficiali", category=category)
            await guild.create_text_channel("💬・taverna-giocatori", category=category)
            stats_channel = await guild.create_text_channel("📊・hub-classifiche", category=category)
            screen_channel = await guild.create_text_channel("📷・invio-risultati", category=category)

            # Invia il menu interattivo direttamente nel canale delle classifiche
            view = MenuClassifica()
            embed = discord.Embed(title="🎮 FraDodo COD Tournament Hub", description="Usa i bottoni sottostanti per visualizzare le tue statistiche o la classifica globale del torneo!", color=discord.Color.blue())
            await stats_channel.send(embed=embed, view=view)
            
            # Invia messaggio di istruzioni nel canale screenshot
            embed_screen = discord.Embed(
                title="📸 Come inviare i risultati", 
                description="In questo canale non ti servono bottoni o comandi complicati!\n\n"
                            "Ti basta **trascinare o incollare l'immagine** della partita terminata direttamente qui in chat (usando il tasto `+` di fianco alla barra per scrivere).\n\n"
                            "Il nostro bot scansionerà automaticamente la foto e ti assegnerà i punti in 1 secondo!\n"
                            "*(In alternativa puoi digitare il comando `/carica_screenshot` per farti aprire la finestra di caricamento).*.", 
                color=discord.Color.purple()
            )
            await screen_channel.send(embed=embed_screen)

            # Crea canali vocali per le squadre
            await guild.create_voice_channel("🔴 Squadra Alpha", category=category)
            await guild.create_voice_channel("🔵 Squadra Bravo", category=category)
            await guild.create_voice_channel("🟢 Squadra Charlie", category=category)
            await guild.create_voice_channel("🟡 Squadra Delta", category=category)

            await interaction.followup.send("✅ Configurazione server completata! Ho creato la categoria e i canali per il torneo.")
        except discord.Forbidden:
            await interaction.followup.send("❌ Non ho i permessi necessari per creare canali/categorie. Assicurati che il bot abbia il permesso 'Gestisci canali'.")
        except Exception as e:
            await interaction.followup.send(f"❌ Si è verificato un errore: {e}")

async def setup(bot):
    await bot.add_cog(Tournament(bot))
    bot.add_view(MenuClassifica())
