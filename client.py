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
# CONFIGURATION
# =========================
with open("config.json", "r") as f:
    config = json.load(f)

DISCONNECTED = "Lost connection with reason"
TIMER = 21600
AUTO_KILL = True
USER_ID = config.get("USER_ID")
BOT_URL = config.get("BOT_URL")
AUTH_TOKEN = config.get("AUTH_TOKEN")

if not USER_ID or not BOT_URL:
    sys.exit("[FATAL] USER_ID or BOT_URL missing in .env")

lock = threading.Lock()
end_time = 0
roblox_running = False

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

def remaining_time():
    return max(0, int(end_time - time.monotonic()))

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
    while True:
        try:
            r = requests.get(f"{BOT_URL}/poll/{USER_ID}", headers=get_auth_header(), timeout=5)
            data = r.json()
            cmds = data.get("commands", [])
            for cmd in cmds:
                if cmd["action"] == "kill":
                    post_event("REMOTE COMMAND", "Kill command received. Closing Roblox...")
                    close_roblox()
                elif cmd["action"] == "status":
                    # Gather status info
                    status = {
                        "title": "Client Status",
                        "description": f"Roblox running: {is_roblox_running()}\nTime left: {hhmmss(remaining_time())}"
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
    global roblox_running, end_time
    threading.Thread(target=poll_commands, daemon=True).start()

    log_dir = get_log_dir()
    if not os.path.exists(log_dir):
        sys.exit(f"[client] Roblox logs folder not found: {log_dir}")

    while True:
        existing_logs = set(glob.glob(os.path.join(log_dir, "*.log")))
        new_log = wait_for_new_log(log_dir, existing_logs)
        roblox_running = True
        end_time = time.monotonic() + TIMER
        post_event("SESSION STARTED", "Waiting for Roblox events...")
        threading.Thread(target=watch_process, daemon=True).start()
        monitor_log(new_log)

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
    global end_time
    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(0, os.SEEK_END)
        while True:
            if not roblox_running:
                break
            line = f.readline()
            if not line:
                time.sleep(1)
                if AUTO_KILL and remaining_time() <= 0:
                    post_event("TIMER EXPIRED", "Disconnect timer expired. Closing Roblox...")
                    close_roblox()
                    end_time = 0  # Reset timer immediately when timer expires
                    break
                continue
            if AUTO_KILL:
                end_time = time.monotonic() + TIMER
            if DISCONNECTED in line:
                post_event("DISCONNECT DETECTED", f"{line.strip()}\nTime left: {hhmmss(remaining_time())}")

def watch_process():
    global roblox_running, end_time
    while True:
        if not is_roblox_running():
            post_event("ROBLOX CLOSED", f"Process ended.\nTime left: {hhmmss(remaining_time())}")
            roblox_running = False
            end_time = 0  # Reset timer when Roblox closes
            break
        time.sleep(2)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[client] Interrupted. Exiting...")
