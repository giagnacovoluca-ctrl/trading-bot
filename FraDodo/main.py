import asyncio
import os
import threading
from dotenv import load_dotenv

import discord
from discord.ext import commands
import uvicorn

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

from database import init_db
init_db()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=["/", "!"], intents=intents)

async def load_cogs():
    await bot.load_extension("cogs.tournament")
    await bot.load_extension("cogs.ocr_verification")
    await bot.load_extension("cogs.ksd_contest")

@bot.event
async def on_ready():
    print(f'Bot is ready. Logged in as {bot.user}')
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Forzata sync di {len(synced)} comandi per {guild.name}")
        except Exception as e:
            print(f"Errore sync per {guild.name}: {e}")

import logging
logging.basicConfig(level=logging.INFO)

async def start_discord_bot():
    async with bot:
        await load_cogs()
        await bot.start(DISCORD_TOKEN)

def run_fastapi():
    uvicorn.run("web.app:app", host="0.0.0.0", port=8000, log_level="info")

async def main():
    api_thread = threading.Thread(target=run_fastapi, daemon=True)
    api_thread.start()

    if not DISCORD_TOKEN or DISCORD_TOKEN == "your_discord_bot_token_here":
        print("Warning: DISCORD_TOKEN is missing or default. Please set it in .env file.")
        print("FastAPI is running. Discord Bot skipped.")
        while True:
            await asyncio.sleep(3600)
        return

    await start_discord_bot()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down...")
