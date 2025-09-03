import json
import requests
import os


import discord
from discord.ext import commands

# ==============================
# LOAD CONFIG
# ==============================
with open("config.json", "r") as f:
    config = json.load(f)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
STATUS_URL = "http://127.0.0.1:5000/status"
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

@bot.command()
async def status(ctx):
    try:
        response = requests.get(STATUS_URL, timeout=3)
        if response.status_code == 200:
            data = response.json()
            await ctx.send(f"✅ Current status: **{data['status']}**")
        else:
            await ctx.send("⚠ Main service returned an error.")
    except requests.exceptions.RequestException:
        await ctx.send("❌ Could not reach main service. Is it running?")

# ==============================
# RUN
# ==============================
if not DISCORD_TOKEN:
    raise SystemExit("Set DISCORD_TOKEN, TARGET_CHANNEL_ID, and PING_CHANNEL_ID in config.json")

bot.run(DISCORD_TOKEN)
