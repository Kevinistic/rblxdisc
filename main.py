import json
import os
import sys
import time
import glob
import platform
import requests
import threading
import psutil
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# =============================
# CONFIGURATION AND GLOBALS
# =============================
load_dotenv()
lock = threading.Lock()
app = Flask(__name__)

DISCONNECTED = "Lost connection with reason"
TIMER = 21600 # 6 hours
AUTO_KILL = True
USER_ID = os.getenv("USER_ID")
BOT_URL = os.getenv("BOT_URL")
AUTH_TOKEN = os.getenv("AUTH_TOKEN")

end_time = 0
roblox_running = False

if not BOT_URL:
    print("[FATAL] BOT_URL is not set (set BOT_URL in your .env or environment).")
    sys.exit(1)

if not USER_ID:
    print("[FATAL] USER_ID is not set (set USER_ID in your .env).")
    sys.exit(1)

if not AUTH_TOKEN:
    print("[WARN] AUTH_TOKEN is not set. /kill endpoint will be unauthenticated.")

# =============================
# FLASK ENDPOINTS
# =============================
@app.route("/kill", methods=["POST"])
def kill_endpoint():
    data = request.get_json(silent=True)
    if not data or data.get("auth_token") != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 403
    killed = close_roblox()
    return jsonify({"killed": killed}), 200

def post_event(title, description):
    payload = {"user_id": USER_ID, "title": title, "description": description}
    try:
        r = requests.post(BOT_URL, json=payload, timeout=5)
        print(f"[main] Event posted: {title}, status={r.status_code}")
        try:
            print("[main] Response JSON:", r.json())
        except Exception:
            pass
    except Exception as e:
        print(f"[main] Failed to post event: {e}")
        print("[main] Payload was:", payload)

# =============================
# HELPER FUNCTIONS
# =============================
def get_log_dir():
    system = platform.system()
    if system == "Windows":
        return os.path.expandvars(r"%LOCALAPPDATA%\Roblox\logs")
    elif system == "Darwin":
        return os.path.expanduser("~/Library/Logs/Roblox")
    else:
        return os.path.expanduser("~/.var/app/org.vinegarhq.Sober/data/sober/sober_logs/")
    
def hhmmss(seconds):
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}h {m}m {s}s"

def remaining_time(end_time):
    now = time.monotonic()
    return max(0, int(end_time - now))

# =============================
# ROBLOX PROCESS MANAGEMENT
# =============================
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
            except Exception:
                pass
    print(f"[main] Killed {killed} Roblox process(es).")
    return killed

def wait_for_new_log(log_dir, existing_logs):
    print("[main] Waiting for Roblox to launch and create a new log file...")
    while True:
        current = set(glob.glob(os.path.join(log_dir, "*.log")))
        new_logs = current - existing_logs
        if new_logs:
            log_file = max(new_logs, key=os.path.getctime)
            print(f"[main] New log detected: {log_file}")
            return log_file
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
                time.sleep(1.0)
                if AUTO_KILL:
                    with lock:
                        cur = remaining_time(end_time)
                    if cur <= 0:
                        post_event("TIMER EXPIRED", "Disconnect timer expired. Closing Roblox...")
                        close_roblox()
                        break
                continue
            if AUTO_KILL:
                with lock:
                    end_time = time.monotonic() + TIMER
            if DISCONNECTED in line:
                with lock:
                    cur = remaining_time(end_time)
                description = f"{line.strip()}\nTime left: {hhmmss(cur)}"
                post_event("DISCONNECT DETECTED", description)

def main():
    global roblox_running, end_time
    log_dir = get_log_dir()
    if not os.path.exists(log_dir):
        sys.exit(f"[main] Roblox logs folder not found: {log_dir}")
    while True:
        existing_logs = set(glob.glob(os.path.join(log_dir, "*.log")))
        new_log = wait_for_new_log(log_dir, existing_logs)
        roblox_running = True
        with lock:
            end_time = time.monotonic() + TIMER
        post_event("SESSION STARTED", "Waiting for Roblox events...")
        threading.Thread(target=watch_process, daemon=True).start()
        monitor_log(new_log)
        print("[main] Roblox session ended. Waiting for next session...")
        time.sleep(2)

def watch_process():
    global roblox_running
    while True:
        if not is_roblox_running():
            with lock:
                time_left = remaining_time(end_time)
            description = f"Roblox process has ended.\nTime left: {hhmmss(time_left)}"
            post_event("ROBLOX CLOSED", description)
            roblox_running = False
            break
        time.sleep(2)

if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=5001, use_reloader=False), daemon=True).start()
    try:
        main()
    except KeyboardInterrupt:
        print("\n[main] Interrupted by user. Exiting...")
        sys.exit(0)