import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import database
import time

import io
from PIL import Image, ImageDraw, ImageFont

async def check_champion_role(guild: discord.Guild):
    # This is a simple implementation: it finds the #1 player, and if they are in the guild, gives them the "👑 Campione KSD" role
    players = database.get_all_players()
    if not players: return
    
    leader_discord_id = players[0]['discord_id']
    role = discord.utils.get(guild.roles, name="👑 Campione KSD")
    
    if not role:
        try:
            role = await guild.create_role(name="👑 Campione KSD", color=discord.Color.gold(), hoist=True, reason="Auto-created Champion Role")
        except:
            return # Missing perms
            
    # Remove from everyone else, add to leader
    for member in guild.members:
        if role in member.roles and str(member.id) != leader_discord_id:
            await member.remove_roles(role)
        elif str(member.id) == leader_discord_id and role not in member.roles:
            await member.add_roles(role)

class InviaRisultatoModal(discord.ui.Modal, title='Invia Risultato Contest'):
    nome_player = discord.ui.TextInput(label='Nome Player (Opzionale)', placeholder='Lascia vuoto per usare il tuo nome', required=False)
    kills = discord.ui.TextInput(label='Kills Totali', placeholder='Es. 15', required=True)
    posizione = discord.ui.TextInput(label='Posizione/Piazzamento', placeholder='Es. 1', required=True)

    async def on_submit(self, interaction: discord.Interaction):
        discord_id = str(interaction.user.id)
        player = database.get_player(discord_id)
        
        nome_inserito = self.nome_player.value.strip() if self.nome_player.value else interaction.user.display_name

        if not player:
            database.register_player(discord_id, nome_inserito)
            player = {'activision_id': nome_inserito}
        else:
            if nome_inserito and nome_inserito != player['activision_id'] and self.nome_player.value.strip() != "":
                # Update name if provided
                pass # per ora manteniamo l'ID originale o potremmo aggiornarlo

        try:
            k = int(self.kills.value)
            p = int(self.posizione.value)
        except ValueError:
            await interaction.response.send_message("❌ Kills e Posizione devono essere numeri validi!", ephemeral=True)
            return
            
        url = "Nessuno (Testo)"
        punti = database.save_contest_match(discord_id, k, p, url)

        log_channel = discord.utils.get(interaction.guild.channels, name="📜│log-contest")
        if log_channel:
            embed = discord.Embed(title="✅ Nuova Partita Registrata (Senza Foto)", color=discord.Color.green())
            embed.add_field(name="Giocatore", value=interaction.user.mention, inline=True)
            embed.add_field(name="Nome", value=nome_inserito, inline=True)
            embed.add_field(name="Kills", value=str(k), inline=True)
            embed.add_field(name="Posizione", value=str(p), inline=True)
            embed.add_field(name="Punti Ottenuti", value=f"+{punti:.1f} (In attesa di conferma)", inline=True)
            await log_channel.send(embed=embed)
            
        if interaction.guild:
            await check_champion_role(interaction.guild)
        
        await interaction.response.send_message(f"✅ Risultato inviato con successo! Punti: **{punti:.1f}** (In attesa di conferma).\n*(Per inviare anche la foto da Discord, usa il comando `/invia_risultato`)*", ephemeral=True)

class InviaScegliMetodoView(discord.ui.View):
    def __init__(self, web_url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="🌐 Carica su Web App (Con Foto)", url=web_url, style=discord.ButtonStyle.link, row=0))

    @discord.ui.button(label="📝 Compila Testo (Senza Foto)", emoji="📝", style=discord.ButtonStyle.secondary, custom_id="ksd_scegli_discord", row=1)
    async def btn_discord(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(InviaRisultatoModal())

class KSDContestMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        current_url = "https://dodo-dashboard-ksd.loca.lt"
        try:
            if os.path.exists("current_url.txt"):
                with open("current_url.txt", "r") as f:
                    content = f.read().strip()
                    if content: current_url = content
        except:
            pass
        self.add_item(discord.ui.Button(label="🌐 Apri Web App", url=current_url, style=discord.ButtonStyle.link, row=2))
        
    @discord.ui.button(label="Invia Risultato", emoji="🟢", style=discord.ButtonStyle.success, custom_id="ksd_invia_risultato", row=0)
    async def btn_invia(self, interaction: discord.Interaction, button: discord.ui.Button):
        discord_id = str(interaction.user.id)
        player = database.get_player(discord_id)
        
        if not player:
            # Auto-iscrizione silenziosa se l'utente non è iscritto
            activision_id = interaction.user.display_name
            database.register_player(discord_id, activision_id)
            player = {'activision_id': activision_id}

        activision_id = player.get('activision_id', interaction.user.display_name)
        
        current_url = "https://dodo-dashboard-ksd.loca.lt"
        try:
            if os.path.exists("current_url.txt"):
                with open("current_url.txt", "r") as f:
                    content = f.read().strip()
                    if content: current_url = content
        except:
            pass

        import urllib.parse
        encoded_name = urllib.parse.quote(activision_id)
        upload_link = f"{current_url}/?user_id={discord_id}&name={encoded_name}#sec-invia"
        
        embed = discord.Embed(
            title="📤 Invia il tuo Risultato",
            description=f"Ciao **{activision_id}**!\n\nScegli come preferisci inviare il risultato:\n\n🌍 **Su Web App (Consigliato):** Ti permette di caricare direttamente la foto dello screenshot dalla tua galleria.\n📝 **Da Discord:** Ti permette di scrivere solo il numero di Kills e Posizione direttamente in chat.",
            color=0x2ECC71
        )
        
        view = InviaScegliMetodoView(web_url=upload_link)
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Classifica", emoji="🏆", style=discord.ButtonStyle.primary, custom_id="ksd_classifica", row=0)
    async def btn_classifica(self, interaction: discord.Interaction, button: discord.ui.Button):
        players = database.get_all_players()
        if not players:
            await interaction.response.send_message("Nessun giocatore iscritto al momento.", ephemeral=True)
            return
            
        embed = discord.Embed(title="🏆 Classifica KSD Contest", color=discord.Color.gold())
        for i, p in enumerate(players[:10]):
            totale = p.get('points', 0) + p.get('contest_points', 0.0)
            embed.add_field(name=f"#{i+1} {p['activision_id']}", value=f"Punti: {totale}", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Profilo", emoji="👤", style=discord.ButtonStyle.secondary, custom_id="ksd_profilo", row=1)
    async def btn_profilo(self, interaction: discord.Interaction, button: discord.ui.Button):
        discord_id = str(interaction.user.id)
        player = database.get_player(discord_id)
        
        if not player:
            # Auto-iscrizione
            activision_id = interaction.user.display_name
            database.register_player(discord_id, activision_id)
            player = database.get_player(discord_id)
            
        totale = player.get('points', 0) + player.get('contest_points', 0.0)
        
        # Genera Immagine Profilo
        img = Image.new('RGB', (600, 250), color=(18, 21, 29))
        d = ImageDraw.Draw(img)
        
        # Accenti grafici
        d.rectangle([0, 0, 15, 250], fill=(88, 101, 242))
        d.rectangle([15, 0, 600, 60], fill=(35, 40, 52))
        
        try:
            font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
            font_text = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
            font_big = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
        except:
            font_title = font_text = font_big = ImageFont.load_default()
            
        d.text((40, 10), "PROFILO GIOCATORE", fill=(255, 255, 255), font=font_title)
        d.text((40, 80), f"ID: {player['activision_id']}", fill=(200, 200, 200), font=font_text)
        d.text((40, 130), "PUNTI CONTEST:", fill=(241, 196, 15), font=font_text)
        d.text((40, 160), f"{totale:.1f}", fill=(241, 196, 15), font=font_big)
        
        d.text((350, 130), "MATCH GIOCATI:", fill=(88, 101, 242), font=font_text)
        d.text((350, 160), f"{player['matches_played']}", fill=(255, 255, 255), font=font_big)
        
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        
        file = discord.File(buf, filename="profilo.png")
        await interaction.response.send_message(file=file, ephemeral=True)

    @discord.ui.button(label="Statistiche", emoji="📊", style=discord.ButtonStyle.secondary, custom_id="ksd_statistiche", row=1)
    async def btn_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("📊 Statistiche generali del contest in arrivo...", ephemeral=True)

    @discord.ui.button(label="Hall Of Fame", emoji="🥇", style=discord.ButtonStyle.secondary, custom_id="ksd_hof", row=1)
    async def btn_hof(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🥇 La Hall of Fame mostra i vincitori storici del server KSD!", ephemeral=True)

    @discord.ui.button(label="Staff", emoji="⚙️", style=discord.ButtonStyle.danger, custom_id="ksd_staff", row=2)
    async def btn_staff(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Solo lo Staff può accedere a questo pannello.", ephemeral=True)
            return
        
        current_url = "https://dodo-dashboard-ksd.loca.lt"
        try:
            if os.path.exists("current_url.txt"):
                with open("current_url.txt", "r") as f:
                    content = f.read().strip()
                    if content: current_url = content
        except:
            pass
            
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="⚙️ Apri Pannello Web", url=f"{current_url}/admin", style=discord.ButtonStyle.danger))
        await interaction.response.send_message("Accedi al pannello di gestione dal browser per accettare/rifiutare risultati.", view=view, ephemeral=True)


class KSDContest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_url = ""
        self.url_updater.start()
        self.highlights_updater.start()

    def cog_unload(self):
        self.url_updater.cancel()
        self.highlights_updater.cancel()

    @tasks.loop(seconds=10)
    async def highlights_updater(self):
        highlights = database.get_pending_highlights()
        if not highlights:
            return
            
        try:
            with open("saved_messages.json", "r") as f:
                data = json.load(f)
            guild_id = data.get("guild_id")
            if not guild_id: return
            guild = self.bot.get_guild(guild_id)
            if not guild: return
            
            # Use log-contest channel
            log_channel = discord.utils.get(guild.channels, name="📜│log-contest")
            if not log_channel: return
            
            for h in highlights:
                # Example: "ha droppato 25 Kills..."
                member = guild.get_member(int(h['discord_id']))
                mention = member.mention if member else h['activision_id']
                
                embed = discord.Embed(
                    title="🚨 RECORD PERSONALE / PARTITA EPICA!",
                    description=f"{mention} {h['message']}",
                    color=discord.Color.red()
                )
                embed.set_thumbnail(url="https://i.imgur.com/GqU3kFp.png") # Just a cool generic icon
                await log_channel.send(embed=embed)
                
                database.mark_highlight_sent(h['id'])
        except Exception as e:
            print(f"[Highlights] Error: {e}")

    @tasks.loop(seconds=15)
    async def url_updater(self):
        if not os.path.exists("current_url.txt"):
            return
        with open("current_url.txt", "r") as f:
            new_url = f.read().strip()
            
        if new_url and new_url != self.last_url:
            print(f"[Bot] URL cambiato da {self.last_url} a {new_url}. Aggiorno i bottoni...")
            self.last_url = new_url
            await self.update_all_discord_buttons(new_url)

    async def update_all_discord_buttons(self, new_url):
        if not os.path.exists("saved_messages.json"):
            return
        
        try:
            with open("saved_messages.json", "r") as f:
                data = json.load(f)
        except:
            return
            
        guild_id = data.get("guild_id")
        if not guild_id: return
        guild = self.bot.get_guild(guild_id)
        if not guild: return
        
        async def update_msg(key, view_builder):
            info = data.get(key)
            if info:
                try:
                    ch = guild.get_channel(info["channel_id"])
                    msg = await ch.fetch_message(info["message_id"])
                    view = view_builder()
                    await msg.edit(view=view)
                except Exception as e:
                    print(f"Errore aggiornamento {key}: {e}")

        def get_contest_view():
            return KSDContestMenu()
            
        def get_classifica_view():
            v = discord.ui.View(timeout=None)
            v.add_item(discord.ui.Button(label="🌐 Apri Classifica Completa (Web)", url=new_url))
            return v
            
        def get_approvazioni_view():
            v = discord.ui.View(timeout=None)
            v.add_item(discord.ui.Button(label="⚙️ Apri Pannello Staff (Web)", url=f"{new_url}/admin", style=discord.ButtonStyle.danger))
            return v
            
        def get_hof_view():
            v = discord.ui.View(timeout=None)
            v.add_item(discord.ui.Button(label="🌐 Apri Hall of Fame (Web)", url=new_url))
            return v
            
        def get_log_view():
            v = discord.ui.View(timeout=None)
            v.add_item(discord.ui.Button(label="🌐 Apri Dashboard (Web)", url=new_url))
            return v
            
        def get_staff_view():
            v = discord.ui.View(timeout=None)
            v.add_item(discord.ui.Button(label="⚙️ Gestione Avanzata (Web)", url=f"{new_url}/admin", style=discord.ButtonStyle.danger))
            return v

        await update_msg("contest_msg", get_contest_view)
        await update_msg("classifica_msg", get_classifica_view)
        await update_msg("approvazioni_msg", get_approvazioni_view)
        await update_msg("hof_msg", get_hof_view)
        await update_msg("log_msg", get_log_view)
        await update_msg("staff_msg", get_staff_view)

    @app_commands.command(name="invia_risultato", description="Invia un risultato caricando lo screenshot direttamente da Discord")
    @app_commands.describe(
        kills="Numero totale di kills",
        posizione="Posizione finale (es. 1 per Vittoria)",
        screenshot="La foto della schermata finale",
        nome_player="Opzionale: Il tuo ID in game"
    )
    async def invia_risultato_cmd(self, interaction: discord.Interaction, kills: int, posizione: int, screenshot: discord.Attachment, nome_player: str = None):
        discord_id = str(interaction.user.id)
        player = database.get_player(discord_id)
        
        nome_inserito = nome_player or (player['activision_id'] if player else interaction.user.display_name)
        
        if not player:
            database.register_player(discord_id, nome_inserito)
            player = {'activision_id': nome_inserito}
            
        if not screenshot.content_type.startswith('image/'):
            await interaction.response.send_message("❌ Il file allegato deve essere un'immagine!", ephemeral=True)
            return
            
        await interaction.response.send_message("⏳ Salvataggio del risultato in corso...", ephemeral=True)
        
        # Download the attachment
        safe_filename = f"discord_{discord_id}_{int(time.time())}.jpg"
        file_path = f"web/static/uploads/{safe_filename}"
        os.makedirs("web/static/uploads", exist_ok=True)
        await screenshot.save(file_path)
        
        screenshot_url = f"/static/uploads/{safe_filename}"
        punti = database.save_contest_match(discord_id, kills, posizione, screenshot_url)
        
        # Invia il log nel canale log-contest
        log_channel = discord.utils.get(interaction.guild.channels, name="📜│log-contest")
        if log_channel:
            embed = discord.Embed(title="✅ Nuova Partita Registrata (Da Discord)", color=discord.Color.green())
            embed.add_field(name="Giocatore", value=interaction.user.mention, inline=True)
            embed.add_field(name="Nome", value=nome_inserito, inline=True)
            embed.add_field(name="Kills", value=str(kills), inline=True)
            embed.add_field(name="Posizione", value=str(posizione), inline=True)
            embed.add_field(name="Punti Ottenuti", value=f"+{punti:.1f} (In attesa di conferma)", inline=True)
            embed.set_thumbnail(url=screenshot.url)
            await log_channel.send(embed=embed)
            
        # Aggiorna ruolo campione
        if interaction.guild:
            await check_champion_role(interaction.guild)
            
        await interaction.edit_original_response(content=f"✅ Risultato inviato con successo e foto salvata! Punti guadagnati: **{punti:.1f}** (In attesa di conferma)")


    @app_commands.command(name="setup_ksd", description="Crea il menu principale del KSD Contest nel canale corrente")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_ksd(self, interaction: discord.Interaction):
        view = KSDContestMenu()
        
        embed = discord.Embed(
            title="🏆 KASH!DO CONTEST v2",
            description="Usa i pulsanti qui sotto per gestire il contest.",
            color=0xF1C40F # Giallo Oro
        )
        embed.add_field(name="🟢 Invia Risultato", value="Invia la tua partita per l'approvazione dello Staff.", inline=False)
        embed.add_field(name="🌐 Web App", value="Apri la Dashboard per le statistiche.", inline=False)
        embed.set_footer(text="KASH!DO Contest Manager v3")
        
        await interaction.response.send_message(embed=embed, view=view)

    @commands.command(name="generamenu")
    @commands.has_permissions(administrator=True)
    async def generamenu(self, ctx):
        view = KSDContestMenu()
        
        embed = discord.Embed(
            title="🏆 KASH!DO CONTEST v2",
            description="Usa i pulsanti qui sotto per gestire il contest.",
            color=0xF1C40F # Giallo Oro
        )
        embed.add_field(name="🟢 Invia Risultato", value="Invia la tua partita per l'approvazione dello Staff.", inline=False)
        embed.add_field(name="🏆 Classifica", value="Visualizza la classifica aggiornata del contest.", inline=False)
        embed.add_field(name="👤 Profilo", value="Controlla le tue statistiche personali.", inline=False)
        embed.add_field(name="📊 Statistiche", value="Visualizza statistiche dettagliate del contest.", inline=False)
        embed.add_field(name="🥇 Hall Of Fame", value="Guarda i migliori giocatori di sempre.", inline=False)
        
        embed.set_footer(text="KASH!DO Contest Manager v3")
        
        await ctx.send(embed=embed, view=view)
        # Cancella il messaggio del comando
        await ctx.message.delete()

    @app_commands.command(name="setup_ksd_canali", description="Crea la struttura di canali e bottoni web")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_ksd_canali(self, interaction: discord.Interaction):
        guild = interaction.guild
        # Risposta temporanea
        await interaction.response.send_message("⏳ Creazione della categoria e dei canali KASH!DO CONTEST v2 in corso...", ephemeral=True)
        await self._create_channels(guild, interaction=interaction)

    @commands.command(name="crea_canali")
    async def crea_canali_text(self, ctx):
        guild = ctx.guild
        if not guild:
            await ctx.send("❌ Devi usare questo comando all'interno di un server, non in chat privata (DM)!")
            return
        msg = await ctx.send("⏳ Creazione della categoria e dei canali KASH!DO CONTEST v2 in corso...")
        await self._create_channels(guild, msg=msg)

    async def _create_channels(self, guild, interaction=None, msg=None):
        # Leggi l'URL corrente
        current_url = self.last_url if self.last_url else "https://dodo-dashboard-ksd.loca.lt"
        
        # Crea Categoria
        category = await guild.create_category("🏆 KASH!DO CONTEST v2")
        
        # Crea Canali Testuali
        contest_ch = await guild.create_text_channel("🏆│contest", category=category)
        classifica_ch = await guild.create_text_channel("📊│classifica", category=category)
        approvazioni_ch = await guild.create_text_channel("✅│approvazioni", category=category)
        hof_ch = await guild.create_text_channel("🥇│hall-of-fame", category=category)
        log_ch = await guild.create_text_channel("📜│log-contest", category=category)
        staff_ch = await guild.create_text_channel("⚙│staff-panel", category=category)
        
        saved_msgs = {"guild_id": guild.id}
        
        # Invia messaggi in ogni canale
        # 1. Classifica
        embed_classifica = discord.Embed(title="📊 Classifica KSD Contest", description="Clicca sul bottone per vedere la classifica completa sulla Web App.", color=0x3498DB)
        view_dash_classifica = discord.ui.View(timeout=None)
        view_dash_classifica.add_item(discord.ui.Button(label="🌐 Apri Classifica Completa (Web)", url=current_url))
        msg_obj = await classifica_ch.send(embed=embed_classifica, view=view_dash_classifica)
        saved_msgs["classifica_msg"] = {"channel_id": classifica_ch.id, "message_id": msg_obj.id}
        
        # 2. Approvazioni
        embed_approvazioni = discord.Embed(title="✅ Approvazioni Staff", description="Clicca sul bottone per accedere al pannello Web e approvare i risultati.", color=0xE74C3C)
        view_dash_approvazioni = discord.ui.View(timeout=None)
        view_dash_approvazioni.add_item(discord.ui.Button(label="⚙️ Apri Pannello Staff (Web)", url=f"{current_url}/admin", style=discord.ButtonStyle.danger))
        msg_obj = await approvazioni_ch.send(embed=embed_approvazioni, view=view_dash_approvazioni)
        saved_msgs["approvazioni_msg"] = {"channel_id": approvazioni_ch.id, "message_id": msg_obj.id}
        
        # 3. Hall of Fame
        embed_hof = discord.Embed(title="🥇 Hall of Fame", description="I migliori di sempre. Apri la Web App per i dettagli.", color=0xF1C40F)
        view_dash_hof = discord.ui.View(timeout=None)
        view_dash_hof.add_item(discord.ui.Button(label="🌐 Apri Hall of Fame (Web)", url=current_url))
        msg_obj = await hof_ch.send(embed=embed_hof, view=view_dash_hof)
        saved_msgs["hof_msg"] = {"channel_id": hof_ch.id, "message_id": msg_obj.id}
        
        # 4. Log Contest
        embed_log = discord.Embed(title="📜 Log Contest", description="Tutti gli aggiornamenti storici. Controlla la Web App per i grafici.", color=0x95A5A6)
        view_dash_log = discord.ui.View(timeout=None)
        view_dash_log.add_item(discord.ui.Button(label="🌐 Apri Dashboard (Web)", url=current_url))
        msg_obj = await log_ch.send(embed=embed_log, view=view_dash_log)
        saved_msgs["log_msg"] = {"channel_id": log_ch.id, "message_id": msg_obj.id}
        
        # 5. Staff Panel
        embed_staff = discord.Embed(
            title="⚙️ Pannello Gestione Avanzata", 
            description="Area riservata allo Staff. Clicca il pulsante qui sotto per gestire gli utenti, espellere giocatori o resettare il torneo dal Pannello Web.", 
            color=0x2C3E50
        )
        view_dash_staff = discord.ui.View(timeout=None)
        view_dash_staff.add_item(discord.ui.Button(label="⚙️ Gestione Avanzata (Web)", url=f"{current_url}/admin", style=discord.ButtonStyle.danger))
        msg_obj = await staff_ch.send(embed=embed_staff, view=view_dash_staff)
        saved_msgs["staff_msg"] = {"channel_id": staff_ch.id, "message_id": msg_obj.id}
        
        # 6. Contest Menu
        view = KSDContestMenu()
        embed = discord.Embed(
            title="🏆 KASH!DO CONTEST v2",
            description="Usa i pulsanti qui sotto per gestire il contest.",
            color=0xF1C40F # Giallo Oro
        )
        embed.add_field(name="🟢 Invia Risultato", value="Invia la tua partita per l'approvazione dello Staff.", inline=False)
        embed.add_field(name="🏆 Classifica", value="Visualizza la classifica aggiornata del contest.", inline=False)
        embed.add_field(name="👤 Profilo", value="Controlla le tue statistiche personali.", inline=False)
        embed.add_field(name="📊 Statistiche", value="Visualizza statistiche dettagliate del contest.", inline=False)
        embed.add_field(name="🥇 Hall Of Fame", value="Guarda i migliori giocatori di sempre.", inline=False)
        embed.set_footer(text="KASH!DO Contest Manager v3")
        
        msg_obj = await contest_ch.send(embed=embed, view=view)
        saved_msgs["contest_msg"] = {"channel_id": contest_ch.id, "message_id": msg_obj.id}
        
        with open("saved_messages.json", "w") as f:
            json.dump(saved_msgs, f)
            
        if interaction:
            await interaction.edit_original_response(content="✅ Categoria e canali creati con successo! Ho anche posizionato il menu interattivo nel canale `🏆│contest`.")
        if msg:
            await msg.edit(content="✅ Categoria e canali creati con successo! Ho anche posizionato il menu interattivo nel canale `🏆│contest`.")

async def setup(bot):
    await bot.add_cog(KSDContest(bot))
    bot.add_view(KSDContestMenu())
