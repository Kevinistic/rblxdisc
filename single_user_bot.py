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
import io
try:
    import pyautogui
    import PIL
    SCREENSHOT_AVAILABLE = True
except ImportError:
    SCREENSHOT_AVAILABLE = False

load_dotenv()

# =========================
# CONFIGURATION
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Validate and parse USER_ID
try:
    user_id_str = os.getenv("USER_ID", "")
    if not user_id_str:
        raise ValueError("USER_ID not provided")
    USER_ID = int(user_id_str)
except (ValueError, TypeError) as e:
    sys.exit("[FATAL] USER_ID must be a valid integer in .env")

FOOTER_TEXT = os.getenv("FOOTER_TEXT", "Roblox Monitor")
FOOTER_ICON = os.getenv("FOOTER_ICON", "")
PING_USER = os.getenv("PING_USER", "true").lower() in ("1", "true", "yes")
LOG_RETENTION = int(os.getenv("LOG_RETENTION", "7"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "3"))  # in hours

if not DISCORD_TOKEN or not USER_ID:
    sys.exit("[FATAL] DISCORD_TOKEN or USER_ID missing in .env")

DISCONNECT_KEYWORDS = (
    "Lost connection with reason",
    "Client has been disconnected with reason",
    "Disconnection Notification.",
    # Add more keywords here in the future
)

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
flag_dc = False
# Track when this bot process started
BOT_START_TIME = time.time()

# Locks for thread safety
log_lock = threading.Lock()
state_lock = threading.Lock()

# =========================
# LOGGING FUNCTIONS
# =========================
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_in_log_rotation = False  # Flag to prevent recursive calls during rotation

def ensure_log_dir():
    """Ensure the logs directory exists."""
    try:
        if not os.path.exists(LOG_DIR):
            os.makedirs(LOG_DIR, exist_ok=True)
    except Exception as e:
        print(f"[FATAL] Failed to create log directory {LOG_DIR}: {e}")
        sys.exit(1)

def log_message(message):
    global log_file, _in_log_rotation
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
                
                # Write rotation message directly without recursion to avoid stack issues
                if not _in_log_rotation:
                    _in_log_rotation = True
                    try:
                        rotation_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Log rotated -> {os.path.basename(rotated)}\n"
                        log_file = os.path.join(LOG_DIR, f"bot_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
                        with open(log_file, "a", encoding="utf-8") as f:
                            f.write(rotation_msg)
                        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Log rotated -> {os.path.basename(rotated)}")
                    finally:
                        _in_log_rotation = False

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
bot = commands.Bot(command_prefix="!", intents=intents, status=discord.Status.invisible, activity=None, help_command=None)

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

# Locks for Roblox detection caching
_last_check_lock = threading.Lock()
_last_check = 0
_last_running = False

def is_roblox_running():
    global _last_check, _last_running
    with _last_check_lock:
        if time.time() - _last_check < 1:
            return _last_running
        _last_check = time.time()
        _last_running = any(
            "roblox" in (p.info.get('name') or "").lower() or 
            (p.info.get('name') or "").lower() == "sober"
            for p in psutil.process_iter(['name'])
        )
        return _last_running

def close_roblox():
    killed = 0
    for proc in psutil.process_iter(['name']):
        name = (proc.info.get('name') or "").lower()
        if "robloxplayerbeta" in name or name == "roblox" or name == "sober":
            try:
                proc.kill()
                killed += 1
            except Exception:
                pass
    return killed

def get_log_dir():
    system = platform.system()
    if system == "Windows":
        return os.path.expandvars(r"%LOCALAPPDATA%\Roblox\logs")
    elif system == "Darwin":
        return os.path.expanduser("~/Library/Logs/Roblox")
    else:
        return os.path.expanduser("~/.var/app/org.vinegarhq.Sober/data/sober/sober_logs/")


def get_roblox_session_start_time():
    """Return a monotonic-based session start time for the currently running
    Roblox process, or None if not found.

    This computes: monotonic_start = time.monotonic() - (time.time() - proc.create_time())
    which lets us calculate elapsed time using monotonic clocks (safer against
    system clock jumps) while deriving the start from the process create time.
    """
    try:
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'create_time']):
            try:
                name = (p.info.get('name') or '').lower()
                if 'roblox' in name or name == 'sober':
                    ct = p.info.get('create_time')
                    if ct:
                        procs.append((p.info['pid'], ct))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not procs:
            return None
        # Choose the earliest create_time (longest-running matching process)
        _, earliest_ct = min(procs, key=lambda x: x[1])
        # Convert to monotonic-based start
        monotonic_start = time.monotonic() - (time.time() - earliest_ct)
        return monotonic_start
    except Exception:
        return None

# ==== Screenshot helper for window capture ====
def capture_window():
    """Capture the Roblox window and return as bytes (PNG) or None if failed.
    
    Attempts to find and capture the Roblox window using pyautogui.
    Returns the image as PNG bytes suitable for Discord upload, or None if
    screenshot unavailable or Roblox window not found.
    """
    if not SCREENSHOT_AVAILABLE:
        log_message("[DEBUG] Screenshot not available (pyautogui/PIL not imported)")
        return None
    
    try:
        import pyautogui
        from PIL import Image
        
        log_message("[DEBUG] Attempting to capture screenshot...")
        
        # Suppress pyautogui's safety pause for speed
        original_pause = pyautogui.PAUSE
        pyautogui.PAUSE = 0.01
        
        try:
            # Capture the primary monitor using PIL (more reliable than pyautogui.screenshot on some systems)
            from PIL import ImageGrab
            screenshot = ImageGrab.grab()
            
            if not screenshot:
                log_message("[WARN] Screenshot captured but image is None")
                return None
            
            log_message(f"[DEBUG] Screenshot captured: {screenshot.size}")
            
            # Convert PIL Image to bytes (PNG format)
            img_bytes = io.BytesIO()
            screenshot.save(img_bytes, format='PNG')
            img_bytes.seek(0)
            
            log_message(f"[DEBUG] Image converted to PNG bytes: {len(img_bytes.getvalue())} bytes")
            return img_bytes
            
        except ImportError:
            # Fallback to pyautogui.screenshot if PIL.ImageGrab not available
            log_message("[DEBUG] PIL.ImageGrab not available, trying pyautogui.screenshot()...")
            screenshot = pyautogui.screenshot()
            
            if not screenshot:
                log_message("[WARN] pyautogui.screenshot() returned None")
                return None
            
            log_message(f"[DEBUG] Screenshot captured via pyautogui: {screenshot.size}")
            
            img_bytes = io.BytesIO()
            screenshot.save(img_bytes, format='PNG')
            img_bytes.seek(0)
            
            log_message(f"[DEBUG] Image converted to PNG bytes: {len(img_bytes.getvalue())} bytes")
            return img_bytes
            
        finally:
            pyautogui.PAUSE = original_pause
            
    except Exception as e:
        log_message(f"[ERROR] Screenshot capture failed: {type(e).__name__}: {e}")
        import traceback
        log_message(f"[ERROR] Traceback: {traceback.format_exc()}")
        return None


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

async def send_session_started_event():
    """Send session started embed with auto-delete after 15s."""
    if bot.is_closed():
        return
    with state_lock:
        mu = monitored_user
    if not mu:
        return
    try:
        embed = discord.Embed(title="SESSION STARTED", description="Roblox started. Monitoring logs...", color=0x00FF00)
        if FOOTER_TEXT:
            embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON or discord.Embed.Empty)
        content = f"<@{USER_ID}>" if PING_USER else None
        await mu.send(content=content, embed=embed, delete_after=15)
        log_message("SESSION STARTED embed sent (will auto-delete in 15s).")
    except Exception as e:
        log_message(f"[WARN] Failed to send session started embed: {e}")

# =========================
# SIGNAL HANDLER (SIGTERM)
# =========================
def handle_sigterm(signum, frame):
    log_message("Received SIGTERM. Shutting down gracefully...")
    # Try to notify user if possible (use safe_dispatch to avoid dead-loop scheduling)
    safe_dispatch(send_event, "BOT SHUTDOWN", "Received SIGTERM, shutting down...")

    # Give Discord message a moment to send
    time.sleep(0.5)

    # Try to close the bot gracefully if loop still running
    try:
        loop = getattr(bot, "loop", None)
        if loop and loop.is_running() and not bot.is_closed():
            # schedule bot.close() onto the running loop and wait for it
            future = asyncio.run_coroutine_threadsafe(bot.close(), loop)
            try:
                future.result(timeout=5)  # Wait up to 5 seconds for clean shutdown
            except Exception as e:
                log_message(f"[WARN] Timeout waiting for bot.close(): {e}")
    except Exception as e:
        log_message(f"[WARN] Failed to schedule bot.close(): {e}")

    log_message("Shutdown complete.")
    # Finally exit the process
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)  # Also handle Ctrl+C on Unix/Linux
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
    global disconnect_timestamp, last_roblox_disconnect_time

    log_message("Resumed connection to Discord!")

    # Atomically read disconnect timestamps
    with state_lock:
        dt_disconnect = disconnect_timestamp
        dt_roblox = last_roblox_disconnect_time

    if not dt_disconnect:
        return

    possibledisconnect = ""

    if dt_roblox:
        try:
            dt1 = datetime.strptime(dt_disconnect, "%Y-%m-%d %H:%M:%S")
            dt2 = datetime.strptime(dt_roblox, "%Y-%m-%d %H:%M:%S")
            delta = abs((dt1 - dt2).total_seconds())
            if delta <= 3600:  # within 1h window, adjust as needed
                possibledisconnect = f"\nPossible Roblox disconnect at {dt_roblox}"
        except Exception:
            pass

    # Use safe_dispatch to avoid scheduling on closed loop
    safe_dispatch(send_event, "BOT DISCONNECTED", f"Reconnected after disconnect at {dt_disconnect}" + possibledisconnect)
    
    with state_lock:
        disconnect_timestamp = None

# =========================
# HEARTBEAT TASK
# =========================
async def send_heartbeat_event():
    """Send heartbeat embed with auto-delete."""
    if bot.is_closed():
        return
    with state_lock:
        mu = monitored_user
    if not mu:
        return
    try:
        embed = discord.Embed(title="HEARTBEAT", description="Bot still active and responsive.", color=0x00FFFF)
        if FOOTER_TEXT:
            embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON or discord.Embed.Empty)
        content = f"<@{USER_ID}>" if PING_USER else None
        await mu.send(content=content, embed=embed, delete_after=15)
        log_message("[HEARTBEAT] Bot alive notification sent (will auto-delete in 15s).")
    except Exception as e:
        log_message(f"[WARN] Failed to send heartbeat: {e}")

@tasks.loop(hours=HEARTBEAT_INTERVAL)
async def heartbeat():
    await send_heartbeat_event()


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
    
    # Prepare the status embed
    status_text = ""
    
    # Attempt to capture screenshot
    screenshot_bytes = capture_window()
    
    # Send embed with optional screenshot
    try:
        embed = discord.Embed(title="CLIENT STATUS", description=status_text)
        if FOOTER_TEXT:
            embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON or discord.Embed.Empty)
        
        # If screenshot available, attach it and reference in embed
        if screenshot_bytes:
            file = discord.File(screenshot_bytes, filename="roblox_screenshot.png")
            embed.set_image(url="attachment://roblox_screenshot.png")
            content = f"<@{USER_ID}>" if PING_USER else None
            await monitored_user.send(content=content, embed=embed, file=file)
            log_message(f"[COMMAND] Status sent with screenshot")
        else:
            # Fallback to embed-only (no screenshot)
            content = f"<@{USER_ID}>" if PING_USER else None
            await monitored_user.send(content=content, embed=embed)
            log_message(f"[COMMAND] Status sent (no screenshot)")
    except Exception as e:
        log_message(f"[ERROR] Failed to send status: {e}")
        log_message(traceback.format_exc())

@bot.command()
@dm_only()
async def kill(ctx):
    log_message(f"Received kill command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return
    killed = close_roblox()
    msg = f"Killed {killed} Roblox process(es)." if killed else "No Roblox processes found to kill."
    await send_event("ROBLOX KILL", msg)
    log_message(f"[COMMAND] {msg}")

@bot.command()
@dm_only()
async def ping(ctx):
    log_message(f"Received ping command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return
    await send_event("PONG", "The bot is active and responsive.")
    log_message("[COMMAND] Pong response sent.")

@bot.command()
@dm_only()
async def shutdown(ctx):
    log_message(f"Received shutdown command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return
    await send_event("BOT SHUTDOWN", "The bot is shutting down as per your request.")
    log_message("Shutting down bot as per user command.")

    await asyncio.sleep(1)
    await bot.close()
    sys.exit(0)


@bot.command()
@dm_only()
async def restart(ctx):
    """Restart the bot process (wrapper should restart it).

    This differs from shutdown: shutdown exits with code 0 (no restart),
    restart exits with a non-zero code so the external wrapper can restart
    the process.
    """
    log_message(f"Received restart command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return
    await send_event("BOT RESTART", "Restarting bot as requested.")
    log_message("Restarting bot as per user command.")

    # Give the message a moment to send
    await asyncio.sleep(1)
    try:
        await bot.close()
    finally:
        # Exit with a non-zero code so the wrapper treats this as a restart
        sys.exit(2)

@bot.command()
@dm_only()
async def uptime(ctx):
    log_message(f"Received uptime command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return

    # OS uptime
    os_start = datetime.fromtimestamp(psutil.boot_time())
    os_uptime_sec = int(time.time() - os_start.timestamp())
    os_uptime = hhmmss(os_uptime_sec)

    # Bot uptime
    try:
        bot_uptime_sec = int(time.time() - BOT_START_TIME)
        bot_uptime = hhmmss(bot_uptime_sec)
    except Exception:
        bot_uptime = "N/A"

    # Roblox uptime (session)
    running = is_roblox_running()
    roblox_uptime = hhmmss(elapsed_time()) if running else "N/A"

    desc = (
        f"Roblox uptime: {roblox_uptime}\n"
        f"Bot uptime: {bot_uptime}\n"
        f"OS uptime: {os_uptime}"
    )

    await send_event("SYSTEM UPTIME", desc)
    log_message(f"[COMMAND] Uptime sent: Roblox={roblox_uptime}, Bot={bot_uptime}, OS={os_uptime}")

@bot.command()
@dm_only()
async def setflag(ctx):
    """Set the disconnect flag to ignore the next detected disconnect."""
    global flag_dc
    log_message(f"Received setflag command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return
    
    flag_dc = not flag_dc
    await send_event(f"DISCONNECT FLAG SET", "The disconnect flag has been set to {flag_dc}.")
    log_message("[COMMAND] Disconnect flag set.")

@bot.command(name='help')
@dm_only()
async def help_command(ctx):
    """Display all available bot commands."""
    log_message(f"Received help command from {ctx.author} ({ctx.author.id})")
    if ctx.author.id != USER_ID:
        return
    
    commands_list = [
        ("!status", "Check Roblox status + screenshot"),
        ("!kill", "Kill Roblox processes"),
        ("!ping", "Test bot responsiveness"),
        ("!shutdown", "Shut down the bot"),
        ("!restart", "Restart the bot"),
        ("!uptime", "Show uptime statistics"),
        ("!setflag", "Toggle disconnect ignore flag"),
        ("!help", "Show this message")
    ]
    
    description = "\n".join([f"`{cmd}` - {desc}" for cmd, desc in commands_list])

    await send_event("AVAILABLE COMMANDS", description)
    log_message("[COMMAND] Help message sent.")

# =========================
# ROBLOX MONITORING TASK
# =========================
@tasks.loop(seconds=1)
async def monitor_roblox():
    global roblox_running, session_start

    running = is_roblox_running()
    with state_lock:
        if running and not roblox_running:
            # Compute a more accurate session start using the Roblox process create_time
            computed_start = get_roblox_session_start_time()
            if computed_start:
                session_start = computed_start
            else:
                # Fallback to monotonic now
                session_start = time.monotonic()
            roblox_running = True
            log_message("Roblox session started")
            # Use safe_dispatch to send SESSION STARTED with auto-delete
            safe_dispatch(send_session_started_event)
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
    global roblox_running, last_roblox_disconnect_time, flag_dc
    
    # Snapshot the session start at thread startup to avoid race with reset
    with state_lock:
        session_start_snapshot = session_start
    
    log_dir = get_log_dir()
    if not os.path.exists(log_dir):
        log_message(f"Roblox logs folder not found: {log_dir}")
        return

    try:
        # Collect the current logs. If Roblox already created a log file before
        # this thread starts (e.g. bot restarted while Roblox running), attach
        # to the most recent existing log. Otherwise wait for a new log to be
        # created.
        existing_logs = set(glob.glob(os.path.join(log_dir, "*.log")))
        new_log = None

        if existing_logs:
            # Attach to the most recent existing log (this handles wrapper restarts)
            new_log = max(existing_logs, key=os.path.getctime)
        else:
            # Wait for the first log to appear
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

        def get_elapsed():
            """Calculate elapsed time from snapshot, avoid race with session_start reset."""
            if session_start_snapshot == 0:
                return 0
            return max(0, int(time.monotonic() - session_start_snapshot))

        # Open and tail the current log, but keep checking for a newer log file
        # (rotation or Roblox creating a fresh file) and switch to it when found.
        current_log = new_log
        f = open(current_log, "r", encoding="utf-8", errors="ignore")
        try:
            f.seek(0, os.SEEK_END)
            while True:
                with state_lock:
                    if not roblox_running:
                        return

                line = f.readline()
                if not line:
                    # Periodically check if a newer log file exists (rotation)
                    time.sleep(1)
                    try:
                        all_logs = glob.glob(os.path.join(log_dir, "*.log"))
                        if not all_logs:
                            continue
                        latest = max(all_logs, key=os.path.getctime)
                        if os.path.abspath(latest) != os.path.abspath(current_log):
                            # Switch to the newer log
                            log_message(f"Detected new log file, switching to: {os.path.basename(latest)}")
                            f.close()
                            current_log = latest
                            f = open(current_log, "r", encoding="utf-8", errors="ignore")
                            f.seek(0, os.SEEK_END)
                            continue
                    except Exception:
                        # Ignore transient filesystem errors
                        pass
                    continue

                if any(keyword in line for keyword in DISCONNECT_KEYWORDS):
                    if flag_dc:
                        flag_dc = False
                        log_message("TP once flag detected, skipping disconnect handling.")
                        continue

                    with state_lock:
                        last_roblox_disconnect_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    log_message(f"Roblox disconnect detected: {line.strip()}")
                    # Use safe_dispatch to avoid scheduling on closed loop
                    safe_dispatch(send_event, "DISCONNECT DETECTED", f"{line.strip()}\nTime elapsed: {hhmmss(get_elapsed())}")
                    close_roblox()
                    break

                if "stop() called" in line:
                    log_message(f"Roblox closed: {line.strip()}")
                    safe_dispatch(send_event, "ROBLOX CLOSED", f"Process ended.\nTime elapsed: {hhmmss(get_elapsed())}")
                    close_roblox()  # DO NOT REMOVE, stop() called does not mean 100% exit
                    break
        finally:
            try:
                f.close()
            except Exception:
                pass
    except Exception:
        log_message(traceback.format_exc())

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    # Note: Auto-restart logic has been moved to wrapper.py
    # This script now runs once; the wrapper handles restarts on crash
    try:
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        log_message("KeyboardInterrupt received, exiting gracefully...")
        sys.exit(0)
    except SystemExit as e:
        log_message(f"SystemExit received with code {e.code}.")
        sys.exit(e.code if isinstance(e.code, int) else 1)
    except Exception:
        log_message(traceback.format_exc())
        sys.exit(1)