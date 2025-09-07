import os
import asyncio
import json
import threading
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify
import discord
from discord.ext import commands

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
USER_ID = os.getenv("USER_ID")  # For DM commands
AUTH_TOKEN = os.getenv("AUTH_TOKEN")  # For kill endpoint security
PING_USER = os.getenv("PING_USER", "true").lower() in ("1", "true", "yes")
FOOTER_TEXT = os.getenv("FOOTER_TEXT")
FOOTER_ICON = os.getenv("FOOTER_ICON")
PORT = int(os.getenv("PORT"))

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
app = Flask(__name__)

last_events = {}  # Stores last event per user_id: { "123456": {"title":..., "desc":...} }

@bot.event
async def on_ready():
    print(f"[BOT] Logged in as {bot.user} ({bot.user.id})")

async def send_dm_embed(user_id: str, title: str, description: str):
    try:
        user = await bot.fetch_user(int(user_id))
        if user is None:
            print(f"[BOT] Could not fetch user {user_id}")
            return False

        embed = discord.Embed(title=title, description=description, color=0xFF0000)
        embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON if FOOTER_ICON else discord.Embed.Empty)

        content = f"<@{user_id}>" if PING_USER else None

        try:
            await user.send(content=content, embed=embed)
            print(f"[BOT] DM sent to {user_id}: {title}")
            return True
        except discord.Forbidden:
            print(f"[BOT] Cannot DM user {user_id} (Forbidden).")
            return False
        except Exception as e:
            print(f"[BOT] Failed to send DM to {user_id}: {e}")
            return False
    except Exception as e:
        print(f"[BOT] Error in send_dm_embed: {e}")
        return False

# Flask endpoint for main.py to post events
@app.route("/event", methods=["POST"])
def receive_event():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid json"}), 400

    user_id = data.get("user_id")
    title = data.get("title", "Roblox Event")
    description = data.get("description", "")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    # Store last event
    last_events[user_id] = {"title": title, "description": description}

    # Send DM
    fut = asyncio.run_coroutine_threadsafe(send_dm_embed(user_id, title, description), bot.loop)
    try:
        succeeded = fut.result(timeout=10)
    except Exception as e:
        print(f"[FLASK] Exception when sending DM: {e}")
        succeeded = False

    return jsonify({"delivered": bool(succeeded)}), 200 if succeeded else 500

# Discord command: !status (DM only)
@bot.command()
async def status(ctx):
    if ctx.guild is not None:
        await ctx.reply("This command can only be used in DMs.", delete_after=5)
        return
    if str(ctx.author.id) != USER_ID:
        await ctx.reply("You are not authorized to use this command.")
        return

    event = last_events.get(USER_ID)
    if not event:
        await ctx.send("No events recorded yet.")
        return

    embed = discord.Embed(title=event["title"], description=event["description"], color=0x00FF00)
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON if FOOTER_ICON else discord.Embed.Empty)
    await ctx.send(embed=embed)

# Discord command: !kill (DM only)
@bot.command()
async def kill(ctx):
    if ctx.guild is not None:
        await ctx.reply("This command can only be used in DMs.", delete_after=5)
        return
    if str(ctx.author.id) != USER_ID:
        await ctx.reply("You are not authorized to use this command.")
        return

    try:
        r = requests.post("http://127.0.0.1:5001/kill", json={"auth_token": AUTH_TOKEN}, timeout=5)
        if r.status_code == 200:
            await ctx.send("✅ Roblox process killed successfully.")
        else:
            await ctx.send(f"⚠ Failed to kill Roblox. Server returned {r.status_code}")
    except Exception as e:
        await ctx.send(f"❌ Error contacting main.py: {e}")

def run_flask():
    app.run(host="127.0.0.1", port=PORT, use_reloader=False)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(DISCORD_TOKEN)
