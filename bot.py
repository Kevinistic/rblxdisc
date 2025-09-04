import json
import requests
import os
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ==============================
# LOAD CONFIG
# ==============================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
STATUS_URL = "http://127.0.0.1:5000/status"
PREFIX = "!"

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True  # Enable in Dev Portal too

last_status = None

# ==============================
# BOT SETUP
# ==============================
bot = commands.Bot(command_prefix=PREFIX, intents=INTENTS)

# ==============================
# FUNCTIONS
# ==============================
def hhmmss(seconds):
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02}h {minutes:02}m {secs:02}s"

# ==============================
# EVENTS & COMMANDS
# ==============================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    check_status.start()

@tasks.loop(seconds=10)  # Check every 10 seconds
async def check_status():
    global last_status
    try:
        response = requests.get(STATUS_URL, timeout=3)
        if response.status_code == 200:
            data = response.json()
            current_status = data['status']
            current_time = data['timer']

            if current_status != last_status:
                last_status = current_status
                channel = bot.get_channel(CHANNEL_ID)
                if channel:
                    await channel.send(f"⚠ **Status Update:** {current_status}")
                    await channel.send(f"⏱ **Timer:** {hhmmss(current_time)}")
        else:
            print("Main.py returned an error status code.")
    except requests.exceptions.RequestException:
        print("Could not reach main.py service.")


@bot.command()
async def status(ctx):
    try:
        response = requests.get(STATUS_URL, timeout=3)
        if response.status_code == 200:
            data = response.json()
            await ctx.send(f"✅ Current status: **{data['status']}**")
            await ctx.send(f"⏱ Timer: **{hhmmss(data['timer'])}**")
        else:
            await ctx.send("⚠ Main service returned an error.")
    except requests.exceptions.RequestException:
        await ctx.send("❌ Could not reach main service. Is it running?")

# ==============================
# RUN
# ==============================

bot.run(DISCORD_TOKEN)
