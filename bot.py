import os
import asyncio
import json
import threading
from dotenv import load_dotenv
from flask import Flask, request, jsonify
import discord
from discord.ext import commands

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
AUTH_TOKEN = os.getenv("AUTH_TOKEN")
PORT = int(os.getenv("PORT", "5000"))
FOOTER_TEXT = os.getenv("FOOTER_TEXT", "Roblox Monitor")
FOOTER_ICON = os.getenv("FOOTER_ICON")
PING_USER = os.getenv("PING_USER", "true").lower() in ("1", "true", "yes")
AUTHORIZED_USERS = [u.strip() for u in os.getenv("AUTHORIZED_USERS", "").split(",") if u.strip()]

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
app = Flask(__name__)

last_events = {}  # { user_id: {"title":..., "description":...} }
command_queue = {}  # { user_id: [commands] }

# =======================
# DISCORD BOT EVENTS
# =======================
@bot.event
async def on_ready():
    print(f"[BOT] Logged in as {bot.user} ({bot.user.id})")

async def send_dm_embed(user_id: str, title: str, description: str):
    try:
        user = await bot.fetch_user(int(user_id))
        if not user:
            return False
        embed = discord.Embed(title=title, description=description, color=0xFF0000)
        embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON or discord.Embed.Empty)
        content = f"<@{user_id}>" if PING_USER else None
        await user.send(content=content, embed=embed)
        return True
    except:
        return False

# =======================
# FLASK ENDPOINTS
# =======================
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

    last_events[user_id] = {"title": title, "description": description}
    fut = asyncio.run_coroutine_threadsafe(send_dm_embed(user_id, title, description), bot.loop)
    try:
        fut.result(timeout=10)
    except:
        pass

    return jsonify({"status": "ok"}), 200

@app.route("/poll/<user_id>", methods=["GET"])
def poll_commands(user_id):
    if user_id not in command_queue:
        return jsonify({"commands": []}), 200
    cmds = command_queue.get(user_id, [])
    command_queue[user_id] = []
    return jsonify({"commands": cmds}), 200

# =======================
# DISCORD COMMANDS
# =======================
@bot.command()
async def status(ctx):
    if ctx.guild:
        await ctx.reply("This command can only be used in DMs.", delete_after=5)
        return
    if str(ctx.author.id) not in AUTHORIZED_USERS:
        await ctx.reply("You are not authorized.")
        return
    event = last_events.get(str(ctx.author.id))
    if not event:
        await ctx.send("No events recorded yet.")
        return
    embed = discord.Embed(title=event["title"], description=event["description"], color=0x00FF00)
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON or discord.Embed.Empty)
    await ctx.send(embed=embed)

@bot.command()
async def kill(ctx):
    if ctx.guild:
        await ctx.reply("This command can only be used in DMs.", delete_after=5)
        return
    uid = str(ctx.author.id)
    if uid not in AUTHORIZED_USERS:
        await ctx.reply("You are not authorized.")
        return
    if uid not in command_queue:
        command_queue[uid] = []
    command_queue[uid].append({"action": "kill"})
    await ctx.send("✅ Kill command queued for your client.")

def run_flask():
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(DISCORD_TOKEN)
