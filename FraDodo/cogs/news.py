import discord
from discord.ext import commands, tasks
from discord import app_commands
import feedparser
import json
import os
import requests
import html

class News(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config_file = "news_config.json"
        self.news_channel_id = None
        self.last_post_url = None
        self.load_config()
        self.news_checker.start()

    def load_config(self):
        if os.path.exists(self.config_file):
            with open(self.config_file, "r") as f:
                data = json.load(f)
                self.news_channel_id = data.get("news_channel_id")
                self.last_post_url = data.get("last_post_url")
                
    def save_config(self):
        with open(self.config_file, "w") as f:
            json.dump({
                "news_channel_id": self.news_channel_id,
                "last_post_url": self.last_post_url
            }, f)

    def cog_unload(self):
        self.news_checker.cancel()

    @tasks.loop(minutes=30)
    async def news_checker(self):
        if not self.news_channel_id:
            return
            
        channel = self.bot.get_channel(self.news_channel_id)
        if not channel:
            return
            
        try:
            # Fake User-Agent to bypass 403 Forbidden
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get("https://charlieintel.com/feed/", headers=headers, timeout=10)
            if response.status_code != 200:
                return
                
            feed = feedparser.parse(response.content)
            
            if not feed.entries:
                return
                
            # Filter entries for Warzone or Call of Duty
            valid_entries = []
            for entry in feed.entries:
                title_lower = entry.title.lower()
                tags = [tag.term.lower() for tag in entry.get('tags', [])]
                
                # Simple filter
                is_cod = any(kw in title_lower for kw in ['warzone', 'call of duty', 'mw3', 'modern warfare', 'patch', 'update', 'black ops'])
                is_cod_tag = any('warzone' in t or 'call of duty' in t for t in tags)
                
                if is_cod or is_cod_tag:
                    valid_entries.append(entry)
                    
            if not valid_entries:
                return
                
            latest = valid_entries[0]
            
            if latest.link == self.last_post_url:
                return # Already posted
                
            # It's a new post!
            self.last_post_url = latest.link
            self.save_config()
            
            # Format the embed
            title = html.unescape(latest.title)
            desc = html.unescape(latest.description)
            # Remove html tags from desc if any
            import re
            desc = re.sub(r'<[^>]+>', '', desc)
            if len(desc) > 300:
                desc = desc[:297] + "..."
                
            embed = discord.Embed(
                title=f"📰 {title}",
                url=latest.link.replace("editors.charlieintel", "www.charlieintel"),
                description=desc,
                color=discord.Color.blue()
            )
            embed.set_footer(text="Fonte: CharlieIntel")
            
            # Check for image
            if 'media_thumbnail' in latest:
                embed.set_image(url=latest.media_thumbnail[0]['url'])
            elif 'media_content' in latest:
                embed.set_image(url=latest.media_content[0]['url'])
                
            await channel.send(embed=embed)
            
        except Exception as e:
            print(f"[News] Errore nel fetching news: {e}")

    @news_checker.before_loop
    async def before_news_checker(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="set_news_channel", description="Imposta questo canale per ricevere le news di Warzone")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_news_channel(self, interaction: discord.Interaction):
        self.news_channel_id = interaction.channel.id
        self.save_config()
        await interaction.response.send_message(f"✅ Canale delle news impostato su {interaction.channel.mention}! Da ora in poi il bot pubblicherà qui le ultime notizie e patch notes.", ephemeral=False)

    @app_commands.command(name="ultime_news", description="Visualizza l'ultima notizia di Warzone immediatamente")
    async def get_latest_news(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get("https://charlieintel.com/feed/", headers=headers, timeout=10)
            feed = feedparser.parse(response.content)
            if not feed.entries:
                await interaction.followup.send("Nessuna notizia trovata.")
                return
                
            latest = feed.entries[0]
            title = html.unescape(latest.title)
            desc = html.unescape(latest.description)
            import re
            desc = re.sub(r'<[^>]+>', '', desc)
            if len(desc) > 300:
                desc = desc[:297] + "..."
                
            embed = discord.Embed(
                title=f"📰 {title}",
                url=latest.link.replace("editors.charlieintel", "www.charlieintel"),
                description=desc,
                color=discord.Color.blue()
            )
            if 'media_thumbnail' in latest:
                embed.set_image(url=latest.media_thumbnail[0]['url'])
            elif 'media_content' in latest:
                embed.set_image(url=latest.media_content[0]['url'])
                
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"Errore: {e}")

async def setup(bot):
    await bot.add_cog(News(bot))
