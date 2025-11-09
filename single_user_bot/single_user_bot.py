import os
import sys
import time
import glob
import platform
import threading
import psutil
import traceback
import signal
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
import asyncio
from datetime import datetime
import aiohttp

load_dotenv()

# =========================
# CONFIGURATION
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
USER_ID = int(os.getenv("USER_ID"))  # Your Discord user ID
FOOTER_TEXT = os.getenv("FOOTER_TEXT", "Roblox Monitor")
FOOTER_ICON = os.getenv("FOOTER_ICON", "")
PING_USER = os.getenv("PING_USER", "true").lower() in ("1", "true", "yes")
LOG_RETENTION = int(os.getenv("LOG_RETENTION", "7"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "3"))  # in hours

if not DISCORD_TOKEN or not USER_ID:
    sys.exit("[FATAL] DISCORD_TOKEN or USER_ID missing in .env")

# =========================
# GLOBAL STATE
# =========================
roblox_running = False
session_start = 0  # monotonic time when Roblox session starts
monitored_user = None  # Discord user object
log_file = None
disconnect_timestamp = None
last_discord_disconnect_time = 0
last_roblox_disconnect_time = None

# Locks for thread safety
log_lock = threading.Lock()
state_lock = threading.Lock()

# =========================
# LOGGING FUNCTIONS
# =========================
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

def ensure_log_dir():
    """Ensure the logs directory exists."""
    try:
        if not os.path.exists(LOG_DIR):
            os.makedirs(LOG_DIR, exist_ok=True)
    except Exception as e:
        print(f"[FATAL] Failed to create log directory {LOG_DIR}: {e}")
        sys.exit(1)

def log_message(message):
    global log_file
    ensure_log_dir()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    print(log_line)

    with log_lock:
        try:
            if not log_file:
                timestamp_fn = datetime.now().strftime("%Y%m%d_%H%M%S")
                log_file = os.path.join(LOG_DIR, f"bot_log_{timestamp_fn}.txt")

            # ==== FIX: Log rotation ====
            if os.path.exists(log_file) and os.path.getsize(log_file) > 5 * 1024 * 1024:
                rotated = log_file.replace(".txt", f"_rotated_{int(time.time())}.txt")
                os.rename(log_file, rotated)
                log_message(f"[INFO] Log rotated -> {os.path.basename(rotated)}")
                log_file = os.path.join(LOG_DIR, f"bot_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")

            with open(log_file, "a", encoding="utf-8") as f:
                f.write(log_line + "\n")
        except Exception:
            traceback.print_exc()

def init_log_file():
    ensure_log_dir()
    log_message("Performing startup log cleanup and initialization...")
    cleanup_old_logs()
    global log_file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(LOG_DIR, f"bot_log_{timestamp}.txt")
    log_message(f"Bot started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

def cleanup_old_logs():
    ensure_log_dir()
    retention_days = LOG_RETENTION
    if retention_days == 0:
        log_message("[INFO] Log retention disabled (keeping all logs).")
        return
    cutoff_time = time.time() - (retention_days * 86400)
    deleted = 0
    for file in glob.glob(os.path.join(LOG_DIR, "bot_log_*.txt")):
        try:
            if os.path.getmtime(file) < cutoff_time:
                os.remove(file)
                deleted += 1
        except Exception:
            log_message(traceback.format_exc())
    log_message(f"[INFO] Log cleanup complete. Deleted {deleted} old log(s).")

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

init_log_file()

# =========================
# DISCORD BOT SETUP
# =========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, status=discord.Status.invisible, activity=None)

# =========================
# DECORATORS
# =========================
def dm_only():
    async def predicate(ctx):
        return ctx.guild is None
    return commands.check(predicate)

# =========================
# HELPER FUNCTIONS
# =========================
def elapsed_time():
    with state_lock:
        if session_start == 0:
            return 0
        return max(0, int(time.monotonic() - session_start))

def hhmmss(seconds):
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}h {m}m {s}s"

_last_check = 0
_last_running = False
def is_roblox_running():
    global _last_check, _last_running
    if time.time() - _last_check < 1:
        return _last_running
    _last_check = time.time()
    _last_running = any(
        "roblox" in (p.info.get('name') or "").lower()
        for p in psutil.process_iter(['name'])
    )
    return _last_running

def close_roblox():
    killed = 0
    for proc in psutil.process_iter(['name']):
        name = (proc.info.get('name') or "").lower()
        if "robloxplayerbeta" in name or name == "roblox":
            try:
                proc.kill()
                killed += 1
            except Exception:
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

# ==== FIX: Safe dispatcher to avoid scheduling coroutines on closed loop ====
def safe_dispatch(coro_func, *args, **kwargs):
    """
    Schedule coro_func(*args, **kwargs) on bot.loop only if the loop is running and not closed.
    coro_func should be a coroutine function (callable), not a coroutine object.
    """
    try:
        loop = getattr(bot, "loop", None)
        if loop is None:
            return
        if not loop.is_running() or bot.is_closed():
            # loop not running or bot closed — skip scheduling
            return
        # create coroutine now and schedule it
        coro = coro_func(*args, **kwargs)
        asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception as e:
        # Avoid traceback spam during shutdown; print a short line instead
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] safe_dispatch failed: {e}")

async def send_event(title, description, color=0xFF0000):
    if bot.is_closed():
        log_message("[WARN] Tried to send event while bot is closed.")
        return
    # local copy to avoid race if monitored_user is reassigned
    with state_lock:
        mu = monitored_user
    if not mu:
        return
    try:
        embed = discord.Embed(title=title, description=description, color=color)
        if FOOTER_TEXT:
            embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON or discord.Embed.Empty)
        content = f"<@{USER_ID}>" if PING_USER else None
        await mu.send(content=content, embed=embed)
    except aiohttp.ClientConnectorError:
        log_message("[WARN] Network unreachable — skipping send_event()")
    except aiohttp.ClientConnectorDNSError:
        log_message("[WARN] DNS lookup failed — offline?")
    except Exception:
        log_message(traceback.format_exc())

# =========================
# SIGNAL HANDLER (SIGTERM)
# =========================
def handle_sigterm(signum, frame):
    log_message("Received SIGTERM. Shutting down gracefully...")
    # Try to notify user if possible (use safe_dispatch to avoid dead-loop scheduling)
    safe_dispatch(send_event, "BOT SHUTDOWN", "Received SIGTERM, shutting down...", 0xFF0000)

    # Try to close the bot gracefully if loop still running
    try:
        loop = getattr(bot, "loop", None)
        if loop and loop.is_running() and not bot.is_closed():
            # schedule bot.close() onto the running loop
            asyncio.run_coroutine_threadsafe(bot.close(), loop)
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Failed to schedule bot.close(): {e}")

    # Finally exit the process
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)
if hasattr(signal, "SIGBREAK"):  # Windows-specific
    signal.signal(signal.SIGBREAK, handle_sigterm)

# =========================
# DISCORD EVENTS
# =========================
@bot.event
async def on_ready():
    global monitored_user
    with state_lock:
        if monitored_user:
            return  # already set

    log_message(f"Logged in as {bot.user} ({bot.user.id})")
    try:
        with state_lock:
            monitored_user = await bot.fetch_user(USER_ID)
        log_message(f"Monitoring user: {monitored_user.name}")
    except Exception:
        log_message(traceback.format_exc())
        sys.exit(1)

    # Start monitor & heartbeat AFTER bot is ready and loop is running
    if not monitor_roblox.is_running():
        monitor_roblox.start()

    # Start heartbeat now that the loop is running
    if not heartbeat.is_running():
        heartbeat.start()

@bot.event
async def on_disconnect():
    global disconnect_timestamp, last_discord_disconnect_time

    now = time.time()
    # ==== FIX: Debounce (10s cooldown) ====
    if now - last_discord_disconnect_time < 10:
        return
    last_discord_disconnect_time = now
    with state_lock:
        if disconnect_timestamp is None:
            disconnect_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_message("Lost connection to Discord!")

@bot.event
async def on_resumed():
    global disconnect_timestamp
    log_message("Resumed connection to Discord!")

    if not disconnect_timestamp:
        return

    possibledisconnect = ""

    with state_lock:
        if last_roblox_disconnect_time:
            try:
                dt1 = datetime.strptime(disconnect_timestamp, "%Y-%m-%d %H:%M:%S")
                dt2 = datetime.strptime(last_roblox_disconnect_time, "%Y-%m-%d %H:%M:%S")
                delta = abs((dt1 - dt2).total_seconds())
                if delta <= 3600:  # within 1h window, adjust as needed
                    possibledisconnect = f"\nPossible Roblox disconnect at {last_roblox_disconnect_time}"
            except Exception:
                pass

    # Use safe_dispatch to avoid scheduling on closed loop
    safe_dispatch(send_event, "BOT DISCONNECTED", f"Reconnected after disconnect at {disconnect_timestamp}" + possibledisconnect, 0xFFA500)
    disconnect_timestamp = None

# =========================
# HEARTBEAT TASK
# =========================
@tasks.loop(hours=HEARTBEAT_INTERVAL)
async def heartbeat():
    await send_event("HEARTBEAT", "Bot still active and responsive.", 0x00FFFF)
    log_message("[HEARTBEAT] Bot alive notification sent.")

@heartbeat.before_loop
async def before_heartbeat():
    await bot.wait_until_ready()
# NOTE: Do NOT call heartbeat.start() at import; it is started in on_ready()

# =========================
# COMMANDS
# =========================
@bot.command()
@dm_only()
async def status(ctx):
    log_message(f"Received status command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return
    running = is_roblox_running()
    elapsed = hhmmss(elapsed_time()) if running else "N/A"
    await send_event("CLIENT STATUS", f"Roblox running: {running}\nTime elapsed: {elapsed}", 0x00FF00 if running else 0xFF0000)
    log_message(f"[COMMAND] Status sent: Roblox running={running}, Time elapsed={elapsed}")

@bot.command()
@dm_only()
async def kill(ctx):
    log_message(f"Received kill command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return
    killed = close_roblox()
    msg = f"Killed {killed} Roblox process(es)." if killed else "No Roblox processes found to kill."
    await send_event("ROBLOX KILL", msg, color=0xFF0000 if killed else 0xAAAAAA)
    log_message(f"[COMMAND] {msg}")

@bot.command()
@dm_only()
async def ping(ctx):
    log_message(f"Received ping command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return
    await send_event("PONG", "The bot is active and responsive.", color=0x00FF00)
    log_message("[COMMAND] Pong response sent.")

@bot.command()
@dm_only()
async def shutdown(ctx):
    log_message(f"Received shutdown command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return
    await send_event("BOT SHUTDOWN", "The bot is shutting down as per your request.", 0xFF0000)
    log_message("Shutting down bot as per user command.")

    await asyncio.sleep(1)
    await bot.close()
    sys.exit(0)

@bot.command()
@dm_only()
async def uptime(ctx):
    log_message(f"Received uptime command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return
    start_time = datetime.fromtimestamp(psutil.boot_time())
    uptime_sec = int(time.time() - start_time.timestamp())
    await send_event("SYSTEM UPTIME", f"Machine uptime: {hhmmss(uptime_sec)}", 0x00FFFF)
    log_message(f"[COMMAND] Uptime sent: {hhmmss(uptime_sec)}")

# =========================
# ROBLOX MONITORING TASK
# =========================
@tasks.loop(seconds=1)
async def monitor_roblox():
    global roblox_running, session_start

    running = is_roblox_running()
    with state_lock:
        if running and not roblox_running:
            roblox_running = True
            session_start = time.monotonic()
            log_message("Roblox session started")
            safe_dispatch(send_event, "SESSION STARTED", "Roblox started. Monitoring logs...", 0x00FF00)
            # Prevent double thread creation
            if not any(t.name == "RobloxLogMonitor" for t in threading.enumerate()):
                threading.Thread(target=monitor_logs_thread, name="RobloxLogMonitor", daemon=True).start()
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
    global roblox_running, last_roblox_disconnect_time
    log_dir = get_log_dir()
    if not os.path.exists(log_dir):
        log_message(f"Roblox logs folder not found: {log_dir}")
        return

    try:
        existing_logs = set(glob.glob(os.path.join(log_dir, "*.log")))
        new_log = None
        while roblox_running:
            current = set(glob.glob(os.path.join(log_dir, "*.log")))
            new_logs = current - existing_logs
            if new_logs:
                new_log = max(new_logs, key=os.path.getctime)
                break
            time.sleep(0.5)
        else:
            return  # Roblox stopped before finding a log

        log_message(f"Monitoring log file: {os.path.basename(new_log)}")

        with open(new_log, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, os.SEEK_END)
            while True:
                with state_lock:
                    if not roblox_running:
                        return
                line = f.readline()
                if not line:
                    time.sleep(1)
                    continue
                if "Lost connection with reason" in line or "Client has been disconnected with reason" in line:
                    with state_lock:
                        last_roblox_disconnect_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    log_message(f"Roblox disconnect detected: {line.strip()}")
                    # Use safe_dispatch to avoid scheduling on closed loop
                    safe_dispatch(send_event, "DISCONNECT DETECTED", f"{line.strip()}\nTime elapsed: {hhmmss(elapsed_time())}", 0xFF0000)
                    close_roblox()
                    break

                if "stop() called" in line:
                    log_message(f"Roblox closed: {line.strip()}")
                    safe_dispatch(send_event, "ROBLOX CLOSED", f"Process ended.\nTime elapsed: {hhmmss(elapsed_time())}", 0xFFA500)
                    close_roblox() # why the fuck?
                    break
    except Exception:
        log_message(traceback.format_exc())

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    # ==== SMART: Auto-restart wrapper (break on graceful exit) ====
    while True:
        try:
            bot.run(DISCORD_TOKEN)
            # bot.run returned normally -> clean exit; stop watchdog
            break
        except KeyboardInterrupt:
            log_message("KeyboardInterrupt received, exiting gracefully...")
            break
        except SystemExit:
            log_message("SystemExit received, stopping watchdog loop.")
            break
        except Exception:
            # If it's a network/connectivity error at startup, give a longer retry gap
            log_message(traceback.format_exc())
            log_message("[WARN] Bot crashed, restarting in 10 seconds...")
            time.sleep(10)
            continue