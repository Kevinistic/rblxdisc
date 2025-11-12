import os
import asyncio
import json
import threading
import time
import secrets
from dotenv import load_dotenv
from flask import Flask, request, jsonify
import discord
from discord.ext import commands

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", "5000"))
FOOTER_TEXT = os.getenv("FOOTER_TEXT")
FOOTER_ICON = os.getenv("FOOTER_ICON")
PING_USER = os.getenv("PING_USER", "true").lower() in ("1", "true", "yes")
ADMIN = [int(x) for x in os.getenv("ADMIN", "").split(",") if x.strip().isdigit()]
DEBUG = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")

TOKENS_FILE = "user_tokens.json"

def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_tokens(tokens):
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f)

def get_token_for_user(user_id):
    tokens = load_tokens()
    return tokens.get(str(user_id))

def set_token_for_user(user_id, token):
    tokens = load_tokens()
    tokens[str(user_id)] = token
    save_tokens(tokens)

def require_auth(user_id):
    auth_header = request.headers.get("Authorization", "")
    expected = get_token_for_user(user_id)
    return expected and auth_header == f"Bearer {expected}"

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
app = Flask(__name__)

last_events = {}  # { user_id: {"title":..., "description":...} }
command_queue = {}  # { user_id: [commands] }
status_responses = {}  # { user_id: {"title":..., "description":...} }

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
    if not require_auth(user_id):
        return jsonify({"error": "unauthorized"}), 401

    last_events[user_id] = {"title": title, "description": description}
    fut = asyncio.run_coroutine_threadsafe(send_dm_embed(user_id, title, description), bot.loop)
    try:
        fut.result(timeout=10)
    except:
        pass

    return jsonify({"status": "ok"}), 200

@app.route("/poll/<user_id>", methods=["GET"])
def poll_commands(user_id):
    if not require_auth(user_id):
        return jsonify({"error": "unauthorized"}), 401
    if user_id not in command_queue:
        return jsonify({"commands": []}), 200
    cmds = command_queue.get(user_id, [])
    command_queue[user_id] = []
    return jsonify({"commands": cmds}), 200

@app.route("/status/<user_id>", methods=["POST"])
def receive_status(user_id):
    if not require_auth(user_id):
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid json"}), 400
    status_responses[user_id] = data
    return jsonify({"status": "ok"}), 200

# =======================
# DISCORD COMMANDS
# =======================
def admin_only():
    async def predicate(ctx):
        if DEBUG and ctx.author.id not in ADMIN:
            try:
                await ctx.reply("bot is not accessible rn vro", delete_after=5)
            except:
                pass
            return False
        return True
    return commands.check(predicate)

@bot.command()
@admin_only()
async def status(ctx):
    if ctx.guild:
        await ctx.reply("This command can only be used in DMs.", delete_after=5)
        return
    uid = str(ctx.author.id)
    if not get_token_for_user(uid):
        await ctx.reply("You are not registered. Use !register to get your client token.", delete_after=5)
        return

    # Send status command to client
    if uid not in command_queue:
        command_queue[uid] = []
    command_queue[uid].append({"action": "status"})
    await ctx.send("✅ Status command queued for your client.", delete_after=5)

    # Wait for client to respond (polling status_responses)
    for _ in range(20):  # Wait up to 10 seconds (20 * 0.5s)
        await asyncio.sleep(0.5)
        if uid in status_responses:
            data = status_responses.pop(uid)
            embed = discord.Embed(
                title=data.get("title", "Client Status"),
                description=data.get("description", ""),
                color=0x00FF00
            )
            embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON or discord.Embed.Empty)
            await ctx.send(embed=embed)
            return
    await ctx.send("❌ No response from client.", delete_after=5)

@bot.command()
@admin_only()
async def kill(ctx):
    if ctx.guild:
        await ctx.reply("This command can only be used in DMs.", delete_after=5)
        return
    uid = str(ctx.author.id)
    if not get_token_for_user(uid):
        await ctx.reply("You are not registered. Use !register to get your client token.", delete_after=5)
        return
    if uid not in command_queue:
        command_queue[uid] = []
    command_queue[uid].append({"action": "kill"})
    await ctx.send("✅ Kill command queued for your client.", delete_after=5)

@bot.command()
@admin_only()
async def register(ctx):
    if ctx.guild:
        await ctx.reply("Please DM me to register.", delete_after=5)
        return
    uid = str(ctx.author.id)
    existing = get_token_for_user(uid)
    if existing:
        await ctx.send(f"Your token is: `{existing}`\nKeep it secret! Use in your client .env as AUTH_TOKEN.")
        return
    token = secrets.token_urlsafe(24)
    set_token_for_user(uid, token)
    await ctx.send(f"Registration successful!\nYour token is: `{token}`\nKeep it secret! Use it in your client .env as AUTH_TOKEN.")

def run_flask():
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(DISCORD_TOKEN)
