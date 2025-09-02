import asyncio
import json
import os

import discord
from discord.ext import commands

# ==============================
# LOAD CONFIG
# ==============================
with open("config.json", "r") as f:
    config = json.load(f)

DISCORD_TOKEN = config.get("DISCORD_TOKEN")
PREFIX = "!"

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True  # Enable in Dev Portal too

# ==============================
# BOT SETUP
# ==============================
bot = commands.Bot(command_prefix=PREFIX, intents=INTENTS)

# ==============================
# EVENTS & COMMANDS
# ==============================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

    if message.author == bot.user:
        return
    if message.channel.id != TARGET_CHANNEL_ID:
        return

    await stop_ping_cycle()

    detected = embed_detect_keywords(message)
    if detected:
        await start_ping_cycle(detected)

# ==============================
# RUN
# ==============================
if not DISCORD_TOKEN:
    raise SystemExit("Set DISCORD_TOKEN, TARGET_CHANNEL_ID, and PING_CHANNEL_ID in config.json")

bot.run(DISCORD_TOKEN)
