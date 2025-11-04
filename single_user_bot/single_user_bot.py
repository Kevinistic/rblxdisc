import os
import sys
import time
import glob
import platform
import threading
import psutil
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
import asyncio
from datetime import datetime

load_dotenv()

# =========================
# CONFIGURATION
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
USER_ID = int(os.getenv("USER_ID"))  # Your Discord user ID
FOOTER_TEXT = os.getenv("FOOTER_TEXT", "Roblox Monitor")
FOOTER_ICON = os.getenv("FOOTER_ICON", "")
PING_USER = os.getenv("PING_USER", "true").lower() in ("1", "true", "yes")
LOG_RETENTION = (os.getenv("LOG_RETENTION", "7"))

if not DISCORD_TOKEN or not USER_ID:
    sys.exit("[FATAL] DISCORD_TOKEN or USER_ID missing in .env")

# =========================
# GLOBAL STATE
# =========================
roblox_running = False
session_start = 0  # monotonic time when Roblox session starts
monitored_user = None  # Discord user object
log_file = None
log_lock = threading.Lock()
disconnect_timestamp = None

# =========================
# LOGGING FUNCTIONS
# =========================
def init_log_file():
    log_message("Performing startup log cleanup and initialization...")
    cleanup_old_logs()
    global log_file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = f"bot_log_{timestamp}.txt"
    log_message(f"Bot started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

def cleanup_old_logs():
    try:
        retention_days = int(LOG_RETENTION)
    except (TypeError, ValueError):
        sys.exit("[FATAL] LOG_RETENTION must be an integer (0 = keep all logs).")

    if retention_days < 0:
        sys.exit("[FATAL] LOG_RETENTION cannot be negative.")

    if retention_days == 0:
        log_message("[INFO] Log retention disabled (keeping all logs).")
        return

    cutoff_time = time.time() - (retention_days * 86400)
    deleted = 0
    for file in glob.glob("bot_log_*.txt"):
        try:
            if os.path.getmtime(file) < cutoff_time:
                os.remove(file)
                deleted += 1
        except Exception as e:
            log_message(f"[WARN] Failed to remove old log {file}: {e}")

    log_message(f"[INFO] Log cleanup complete. Deleted {deleted} log(s) older than {retention_days} days.")

def log_message(message):
    global log_file
    if not log_file:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = f"bot_log_{timestamp}.txt"
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    
    # Print to console
    print(log_line)
    
    # Write to file
    with log_lock:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(log_line + "\n")
        except Exception as e:
            print(f"Failed to write to log file: {e}")

def get_logs_between_disconnect_and_resume():
    """
    Retrieve the most recent 'Lost connection to Discord' and
    the Roblox disconnect line just above it, scanning bottom-up.
    """
    if not log_file or not disconnect_timestamp:
        return None, []

    try:
        with open(log_file, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]

        # Reverse scan
        discord_disconnect_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if "Lost connection to Discord" in lines[i]:
                discord_disconnect_idx = i
                break

        if discord_disconnect_idx is None:
            log_message("[WARN] No recent Discord disconnect found.")
            return None, []

        # Find the most recent Roblox disconnect before that
        roblox_disconnect_idx = None
        for j in range(discord_disconnect_idx - 1, -1, -1):
            if "Roblox disconnect detected" in lines[j]:
                roblox_disconnect_idx = j
                break

        # Define the segment range to include both (if found)
        start_idx = roblox_disconnect_idx if roblox_disconnect_idx is not None else discord_disconnect_idx
        segment = lines[start_idx:discord_disconnect_idx + 1]

        # Include any logs after disconnect (downward from it)
        logs_after_disconnect = lines[discord_disconnect_idx + 1:] if discord_disconnect_idx + 1 < len(lines) else []

        return lines[discord_disconnect_idx], segment + logs_after_disconnect

    except Exception as e:
        log_message(f"Failed to read log file: {e}")
        return None, []

# =========================
# SINGLE INSTANCE CHECK
# =========================
def is_another_instance_running():
    current_pid = os.getpid()
    this_file = os.path.abspath(__file__)
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['pid'] == current_pid:
                continue
            cmdline = proc.info.get('cmdline')
            if not cmdline:
                continue
            for arg in cmdline:
                if os.path.abspath(arg) == this_file:
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False

if is_another_instance_running():
    print("[bot] Another instance is already running. Exiting.")
    sys.exit(1)

# Initialize log file
init_log_file()

# =========================
# DISCORD BOT SETUP
# =========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, status=discord.Status.invisible, activity=None)

# =========================
# HELPER FUNCTIONS
# =========================
def elapsed_time():
    if session_start == 0:
        return 0
    return max(0, int(time.monotonic() - session_start))

def hhmmss(seconds):
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}h {m}m {s}s"

def is_roblox_running():
    for proc in psutil.process_iter(['name']):
        name = (proc.info.get('name') or "")
        if "RobloxPlayerBeta" in name or "Roblox" in name or "sober" in name:
            return True
    return False

def close_roblox():
    killed = 0
    for proc in psutil.process_iter(['name']):
        name = (proc.info.get('name') or "")
        if "RobloxPlayerBeta" in name or "Roblox" in name or "sober" in name:
            try:
                proc.kill()
                killed += 1
            except:
                pass
    return killed

def get_log_dir():
    system = platform.system()
    if system == "Windows":
        return os.path.expandvars(r"%LOCALAPPDATA%\\Roblox\\logs")
    elif system == "Darwin":
        return os.path.expanduser("~/Library/Logs/Roblox")
    else:
        return os.path.expanduser("~/.var/app/org.vinegarhq.Sober/data/sober/sober_logs/")

async def send_event(title, description, color=0xFF0000):
    if not monitored_user:
        return
    try:
        embed = discord.Embed(title=title, description=description, color=color)
        if FOOTER_TEXT:
            embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON or discord.Embed.Empty)
        content = f"<@{USER_ID}>" if PING_USER else None
        await monitored_user.send(content=content, embed=embed)
    except Exception as e:
        log_message(f"Failed to send event: {e}")

# =========================
# DISCORD BOT EVENTS
# =========================
@bot.event
async def on_ready():
    global monitored_user, disconnect_timestamp
    log_message(f"Logged in as {bot.user} ({bot.user.id})")
    
    try:
        monitored_user = await bot.fetch_user(USER_ID)
        log_message(f"Monitoring user: {monitored_user.name}")
    except Exception as e:
        log_message(f"Failed to fetch user {USER_ID}: {e}")
        sys.exit(1)
    
    if not monitor_roblox.is_running():
        monitor_roblox.start()

@bot.event
async def on_disconnect():
    global disconnect_timestamp
    if disconnect_timestamp is None:
        disconnect_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_message("Lost connection to Discord!")

@bot.event
async def on_resumed():
    global disconnect_timestamp
    log_message("Resumed connection to Discord!")

    # Only proceed if a disconnect was recorded
    if not disconnect_timestamp:
        return

    disconnect_line, logs_after = get_logs_between_disconnect_and_resume()
    if disconnect_line:
        description_lines = [disconnect_line]
        if logs_after:
            description_lines.append("\n**Logs during disconnect:**")
            for log in logs_after[:-1]:
                description_lines.append(log)
        description_lines.append(
            logs_after[-1] if logs_after else f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Reconnected successfully!"
        )
        await send_event("BOT DISCONNECTED", "\n".join(description_lines), color=0xFFA500)
        log_message("[EVENT] BOT DISCONNECTED event sent after resume.")
    
    # Reset timestamp so it won't repeat
    disconnect_timestamp = None

# =========================
# DISCORD COMMANDS
# =========================
@bot.command()
async def status(ctx):
    log_message(f"Received status command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return
    
    if ctx.guild:
        await ctx.reply("This command can only be used in DMs.", delete_after=5)
        return
    
    running = is_roblox_running()
    elapsed = hhmmss(elapsed_time()) if running else "N/A"

    await send_event("CLIENT STATUS",
                     f"Roblox running: {running}\nTime elapsed: {elapsed}",
                     color=0x00FF00 if running else 0xFF0000)
    log_message(f"[COMMAND] Status sent: Roblox running={running}, Time elapsed={elapsed}")

@bot.command()
async def kill(ctx):
    log_message(f"Received kill command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return
    
    if ctx.guild:
        await ctx.reply("This command can only be used in DMs.", delete_after=5)
        return
    
    killed = close_roblox()
    if killed > 0:
        msg = f"Killed {killed} Roblox process(es)."
        await send_event("ROBLOX KILLED", msg, color=0xFF0000)
    else:
        msg = "No Roblox processes found to kill."
        await send_event("ROBLOX KILL ATTEMPT", msg, color=0xAAAAAA)
    
    log_message(f"[COMMAND] {msg}")

@bot.command()
async def ping(ctx):
    log_message(f"Received ping command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return
    
    if ctx.guild:
        await ctx.reply("This command can only be used in DMs.", delete_after=5)
        return
    
    await send_event("PONG", "The bot is active and responsive.", color=0x00FF00)
    log_message("[COMMAND] Pong response sent.")

@bot.command()
async def shutdown(ctx):
    log_message(f"Received shutdown command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return
    
    if ctx.guild:
        await ctx.reply("This command can only be used in DMs.", delete_after=5)
        return
    
    await send_event("BOT SHUTDOWN", "The bot is shutting down as per your request.", color=0xFF0000)
    log_message("Shutting down bot as per user command.")

    await asyncio.sleep(1)
    await bot.close()
    sys.exit(0)

# =========================
# ROBLOX MONITORING TASK
# =========================
@tasks.loop(seconds=1)
async def monitor_roblox():
    global roblox_running, session_start
    
    running = is_roblox_running()
    
    if running and not roblox_running:
        roblox_running = True
        session_start = time.monotonic()
        log_message("Roblox session started")
        await send_event("SESSION STARTED", "Roblox started. Monitoring for events...", color=0x00FF00)
        threading.Thread(target=monitor_logs_thread, daemon=True).start()
    
    elif not running and roblox_running:
        roblox_running = False
        session_start = 0
        await asyncio.sleep(0.1)

@monitor_roblox.before_loop
async def before_monitor():
    await bot.wait_until_ready()

# =========================
# LOG MONITORING
# =========================
def monitor_logs_thread():
    global roblox_running, session_start
    
    log_dir = get_log_dir()
    if not os.path.exists(log_dir):
        log_message(f"Roblox logs folder not found: {log_dir}")
        return
    
    try:
        existing_logs = set(glob.glob(os.path.join(log_dir, "*.log")))
        new_log = None
        while roblox_running and not new_log:
            current = set(glob.glob(os.path.join(log_dir, "*.log")))
            new_logs = current - existing_logs
            if new_logs:
                new_log = max(new_logs, key=os.path.getctime)
                break
            time.sleep(0.5)
        
        if not new_log:
            return
        
        log_message(f"Monitoring log file: {os.path.basename(new_log)}")
        
        with open(new_log, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, os.SEEK_END)
            while roblox_running:
                line = f.readline()
                if not line:
                    time.sleep(1)
                    continue
                
                if "Lost connection with reason" in line or "Client has been disconnected with reason" in line:
                    log_message(f"Roblox disconnect detected: {line.strip()}")
                    asyncio.run_coroutine_threadsafe(
                        send_event("DISCONNECT DETECTED", f"{line.strip()}\nTime elapsed: {hhmmss(elapsed_time())}", color=0xFF0000),
                        bot.loop
                    )
                    break
                
                if "stop() called" in line:
                    log_message(f"Roblox closed: {line.strip()}")
                    asyncio.run_coroutine_threadsafe(
                        send_event("ROBLOX CLOSED", f"Process ended.\nTime elapsed: {hhmmss(elapsed_time())}", color=0xFFA500),
                        bot.loop
                    )
                    break
    
    except Exception as e:
        log_message(f"Exception in monitor_logs_thread: {e}")

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        log_message("Interrupted. Exiting...")
