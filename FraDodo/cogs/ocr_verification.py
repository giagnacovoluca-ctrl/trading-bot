import discord
from discord.ext import commands
from discord import app_commands
import database
import re
import asyncio
from ocr_module import reader

class OcrVerification(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="carica_screenshot", description="Carica uno screenshot di fine partita come fallback se le API falliscono")
    async def carica_screenshot(self, interaction: discord.Interaction, immagine: discord.Attachment):
        discord_id = str(interaction.user.id)
        player = database.get_player(discord_id)
        
        if not player:
            await interaction.response.send_message("❌ Devi essere iscritto per caricare uno screenshot. Usa `/iscriviti`.", ephemeral=True)
            return

        if not immagine.content_type or not immagine.content_type.startswith("image/"):
            await interaction.response.send_message("❌ Il file inviato non è un'immagine valida.", ephemeral=True)
            return

        await interaction.response.defer()
        img_bytes = await immagine.read()
        
        try:
            # Run OCR in a separate thread to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, reader.readtext, img_bytes, 1) 
            text_lines = [r[1].lower() for r in result]
            confidence_list = [r[2] for r in result]
            avg_confidence = sum(confidence_list) / len(confidence_list) if confidence_list else 0
            
            kills, damage, placement = 0, 0, 0
            full_text = " ".join(text_lines)
            
            # Simple heuristic regex parsing
            kills_match = re.search(r'(?:uccisioni|kills)[^\d]*(\d+)', full_text)
            if kills_match: kills = int(kills_match.group(1))
                
            dmg_match = re.search(r'(?:danni|damage)[^\d]*(\d+)', full_text)
            if dmg_match: damage = int(dmg_match.group(1))
                
            place_match = re.search(r'(?:posizione|placement|piazzamento)[^\d]*(\d+)', full_text)
            if place_match: placement = int(place_match.group(1))

            # Logic for review queue
            status = "pending_review" if avg_confidence < 0.8 or (kills == 0 and damage == 0) else "approved"

            database.save_match(
                discord_id=discord_id,
                kills=kills,
                damage=damage,
                placement=placement,
                status=status,
                screenshot_url=immagine.url,
                ocr_confidence=avg_confidence
            )

            if status == "approved":
                await interaction.followup.send(
                    f"✅ **Dati estratti con successo e approvati automaticamente!**\n"
                    f"🔫 Kills: {kills}\n💥 Danni: {damage}\n🏆 Piazzamento: {placement}\n"
                    f"(Confidenza OCR media: {avg_confidence:.2f})"
                )
            else:
                await interaction.followup.send(
                    f"⚠️ **I dati estratti non sono sicuri al 100% (Confidenza {avg_confidence:.2f}).**\n"
                    f"Valori letti: Kills {kills}, Danni {damage}, Piazzamento {placement}.\n"
                    f"Lo screenshot è stato inviato al Pannello Admin per la **Revisione Manuale**."
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Errore imprevisto durante l'analisi OCR: {str(e)}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # Ascolta solo nel canale degli screenshot
        if "invio-risultati" not in message.channel.name.lower():
            return

        # Verifica se ci sono allegati immagine
        if not message.attachments:
            return

        immagine = message.attachments[0]
        if not immagine.content_type or not immagine.content_type.startswith("image/"):
            return

        discord_id = str(message.author.id)
        player = database.get_player(discord_id)
        
        if not player:
            await message.reply("❌ Devi essere iscritto per caricare uno screenshot. Usa `/iscriviti` in chat.", delete_after=10)
            return

        # Mostra che il bot sta scrivendo mentre fa l'OCR
        async with message.channel.typing():
            img_bytes = await immagine.read()
            
            try:
                # Run OCR in a separate thread to avoid blocking the event loop
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, reader.readtext, img_bytes, 1) 
                text_lines = [r[1].lower() for r in result]
                confidence_list = [r[2] for r in result]
                avg_confidence = sum(confidence_list) / len(confidence_list) if confidence_list else 0
                
                kills, damage, placement = 0, 0, 0
                full_text = " ".join(text_lines)
                
                # Simple heuristic regex parsing
                kills_match = re.search(r'(?:uccisioni|kills)[^\d]*(\d+)', full_text)
                if kills_match: kills = int(kills_match.group(1))
                    
                dmg_match = re.search(r'(?:danni|damage)[^\d]*(\d+)', full_text)
                if dmg_match: damage = int(dmg_match.group(1))
                    
                place_match = re.search(r'(?:posizione|placement|piazzamento)[^\d]*(\d+)', full_text)
                if place_match: placement = int(place_match.group(1))

                # Logic for review queue
                status = "pending_review" if avg_confidence < 0.8 or (kills == 0 and damage == 0) else "approved"

                database.save_match(
                    discord_id=discord_id,
                    kills=kills,
                    damage=damage,
                    placement=placement,
                    status=status,
                    screenshot_url=immagine.url,
                    ocr_confidence=avg_confidence
                )

                if status == "approved":
                    await message.reply(
                        f"✅ **Dati estratti automaticamente con successo!**\n"
                        f"🔫 Kills: {kills}\n💥 Danni: {damage}\n🏆 Piazzamento: {placement}\n"
                        f"(Confidenza OCR media: {avg_confidence:.2f})"
                    )
                else:
                    await message.reply(
                        f"⚠️ **I dati estratti dallo screenshot non sono sicuri al 100% (Confidenza {avg_confidence:.2f}).**\n"
                        f"Valori letti: Kills {kills}, Danni {damage}, Piazzamento {placement}.\n"
                        f"Lo screenshot è stato inviato al Pannello Admin per la **Revisione Manuale**."
                    )
            except Exception as e:
                await message.reply(f"❌ Errore imprevisto durante l'analisi OCR: {str(e)}")

async def setup(bot):
    await bot.add_cog(OcrVerification(bot))
