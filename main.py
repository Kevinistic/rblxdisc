import json
import os
import sys
import time
import glob
import platform
import requests
import threading
import psutil
from flask import Flask, jsonify

# ==============================
# LOAD CONFIG
# ==============================
with open("config.json", "r") as f:
    config = json.load(f)

DISCONNECTED = config.get("DISCONNECTED")
USER_ID = config.get("USER_ID")
TIMER = config.get("TIMER")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

disconnect_timer = TIMER
roblox_running = False
lock = threading.Lock()

latest_event = {"status": "Waiting for Roblox events..."}

app = Flask(__name__)

@app.route("/status")
def get_status():
    return jsonify(latest_event)

def send_webhook(message):
    data = {"content": message}
    try:
        requests.post(WEBHOOK_URL, json=data)
    except Exception as e:
        print(f"Webhook error: {e}")

def get_log_dir():
    system = platform.system()
    if system == "Windows":
        return os.path.expandvars(r"%LOCALAPPDATA%\Roblox\logs")
    elif system == "Darwin":  # macOS
        return os.path.expanduser("~/Library/Logs/Roblox")
    else:  # Linux (Sober/Wine)
        return os.path.expanduser("~/.var/app/org.vinegarhq.Sober/data/sober/sober_logs/")

# ==============================
# ROBLOX PROCESS CHECK
# ==============================
def is_roblox_running():
    for proc in psutil.process_iter(['name']):
        if proc.info['name'] and (
            "RobloxPlayerBeta" in proc.info['name'] or
            "Roblox" in proc.info['name'] or
            "sober" in proc.info['name']
        ):
            return True
    return False

def watch_process():
    global roblox_running
    while True:
        if not is_roblox_running():
            event = "[ROBLOX CLOSED] Process ended"
            print(event)
            latest_event["status"] = event
            send_webhook(event)
            with lock:
                roblox_running = False
            break
        time.sleep(2)

# ==============================
# KILL ROBLOX PROCESS
# ==============================
def close_roblox():
    for proc in psutil.process_iter(['name']):
        if proc.info['name'] and (
            "RobloxPlayerBeta" in proc.info['name'] or
            "Roblox" in proc.info['name'] or
            "sober" in proc.info['name']
        ):
            print(f"Killing process: {proc.info['name']}")
            proc.kill()

# ==============================
# LOG MONITORING
# ==============================
def wait_for_new_log(log_dir, existing_logs):
    print("Waiting for Roblox to launch and create a new log file...")
    while True:
        current_logs = set(glob.glob(os.path.join(log_dir, "*.log")))
        new_logs = current_logs - existing_logs
        if new_logs:
            new_log = max(new_logs, key=os.path.getctime)
            print(f"New Roblox log detected: {new_log}")
            return new_log
        time.sleep(0.5)

def monitor_log(log_file):
    global disconnect_timer
    print(f"Monitoring: {log_file}")
    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(0, os.SEEK_END)
        while True:
            with lock:
                if not roblox_running:
                    print("Stopping log monitoring because Roblox closed.")
                    break

            line = f.readline()
            if disconnect_timer <= 0:
                event = "Disconnect timer expired. Closing Roblox..."
                print(event)
                latest_event["status"] = event
                send_webhook(event)
                close_roblox()
                break

            if not line:
                time.sleep(1.0)
                disconnect_timer -= 1
                continue

            if DISCONNECTED in line:
                event = f"[DISCONNECT DETECTED] {line.strip()}"
                print(event)
                latest_event["status"] = event
                send_webhook(event)
                disconnect_timer = TIMER  # Reset timer

# ==============================
# MAIN FUNCTION
# ==============================
def main():
    global roblox_running

    log_dir = get_log_dir()
    if not os.path.exists(log_dir):
        print(f"Roblox logs folder not found: {log_dir}")
        sys.exit(1)

    existing_logs = set(glob.glob(os.path.join(log_dir, "*.log")))
    new_log = wait_for_new_log(log_dir, existing_logs)

    with lock:
        roblox_running = True

    # Start process watcher in background
    threading.Thread(target=watch_process, daemon=True).start()

    monitor_log(new_log)

if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=5000, use_reloader=False)).start()
    main()
