import os
import sys
import time
import json
import glob
import platform
import requests
import threading
import psutil

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
            # Check if any part of the command line matches this script
            for arg in cmdline:
                if os.path.abspath(arg) == this_file:
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False

if is_another_instance_running():
    print("[client] Another instance of client.py is already running. Exiting.")
    sys.exit(1)

# =========================
# CONFIGURATION
# =========================
with open("config.json", "r") as f:
    config = json.load(f)

USER_ID = config.get("USER_ID")
BOT_URL = config.get("BOT_URL")
AUTH_TOKEN = config.get("AUTH_TOKEN")

if not USER_ID or not BOT_URL:
    sys.exit("[FATAL] USER_ID or BOT_URL missing in .env")

lock = threading.Lock()
roblox_running = False
session_start = 0  # monotonic time when Roblox session starts

# =========================
# HELPERS
# =========================
def get_auth_header():
    return {"Authorization": f"Bearer {AUTH_TOKEN}"} if AUTH_TOKEN else {}

def post_event(title, description):
    payload = {"user_id": USER_ID, "title": title, "description": description}
    try:
        requests.post(f"{BOT_URL}/event", json=payload, headers=get_auth_header(), timeout=5)
    except Exception as e:
        print(f"[client] Failed to post event: {e}")


def elapsed_time():
    if session_start == 0:
        return 0
    return max(0, int(time.monotonic() - session_start))

def hhmmss(seconds):
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}h {m}m {s}s"

# =========================
# ROBLOX CONTROL
# =========================
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

# =========================
# COMMAND POLLER
# =========================
def poll_commands():
    global roblox_running, session_start
    while True:
        # Always check if Roblox is running and session_start is not set
        running = is_roblox_running()
        if running and session_start == 0:
            session_start = time.monotonic()
            roblox_running = True
        elif not running:
            roblox_running = False
            session_start = 0
        try:
            r = requests.get(f"{BOT_URL}/poll/{USER_ID}", headers=get_auth_header(), timeout=5)
            data = r.json()
            cmds = data.get("commands", [])
            for cmd in cmds:
                if cmd["action"] == "kill":
                    post_event("REMOTE COMMAND", "Kill command received. Closing Roblox...")
                    close_roblox()
                elif cmd["action"] == "status":
                    status = {
                        "title": "Client Status",
                        "description": f"Roblox running: {roblox_running}\nTime elapsed: {hhmmss(elapsed_time())}"
                    }
                    try:
                        requests.post(f"{BOT_URL}/status/{USER_ID}", json=status, headers=get_auth_header(), timeout=5)
                    except Exception as e:
                        print(f"[client] Failed to post status: {e}")
        except:
            pass
        time.sleep(5)

# =========================
# MAIN LOGIC
# =========================
def main():
    global roblox_running, session_start
    poller_started = getattr(main, "_poller_started", False)
    if not poller_started:
        threading.Thread(target=poll_commands, daemon=True).start()
        main._poller_started = True

    log_dir = get_log_dir()
    if not os.path.exists(log_dir):
        sys.exit(f"[client] Roblox logs folder not found: {log_dir}")

    while True:
        try:
            # Wait for Roblox to start (process appears)
            while not is_roblox_running():
                roblox_running = False
                session_start = 0
                time.sleep(1)
            roblox_running = True
            session_start = time.monotonic()
            post_event("SESSION STARTED", "Waiting for Roblox events...")

            existing_logs = set(glob.glob(os.path.join(log_dir, "*.log")))
            new_log = wait_for_new_log(log_dir, existing_logs)
            monitor_log(new_log)
            # RESTARTING...
        except Exception as e:
            print(f"[client] Exception in main loop: {e}")
            time.sleep(2)

def get_log_dir():
    system = platform.system()
    if system == "Windows":
        return os.path.expandvars(r"%LOCALAPPDATA%\Roblox\logs")
    elif system == "Darwin":
        return os.path.expanduser("~/Library/Logs/Roblox")
    else:
        return os.path.expanduser("~/.var/app/org.vinegarhq.Sober/data/sober/sober_logs/")

def wait_for_new_log(log_dir, existing_logs):
    while True:
        current = set(glob.glob(os.path.join(log_dir, "*.log")))
        new_logs = current - existing_logs
        if new_logs:
            return max(new_logs, key=os.path.getctime)
        time.sleep(0.5)

def monitor_log(log_file):
    global roblox_running, session_start
    f = None
    try:
        f = open(log_file, "r", encoding="utf-8", errors="ignore")
        f.seek(0, os.SEEK_END)
        while True:
            if not roblox_running:
                break
            line = f.readline()
            if not line:
                time.sleep(1)
                continue
            if "Lost connection with reason" in line or "Client has been disconnected with reason" in line: # dc
                post_event("DISCONNECT DETECTED", f"{line.strip()}\nTime elapsed: {hhmmss(elapsed_time())}")
                break
            if "stop() called" in line: # closed
                post_event("ROBLOX CLOSED", f"Process ended.\nTime elapsed: {hhmmss(elapsed_time())}")
                roblox_running = False
                session_start = 0
                break
    except Exception as e:
        print(f"[client] Exception in monitor_log: {e}")
    finally:
        if f:
            f.close()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[client] Interrupted. Exiting...")
