import json
import os
import sys
import time
import glob
import platform
import threading
import psutil

# ==============================
# LOAD CONFIG
# ==============================
with open("config.json", "r") as f:
    config = json.load(f)

DISCONNECTED = config.get("DISCONNECTED")
CLOSED = config.get("CLOSED")
USER_ID = config.get("USER_ID")
TIMER = config.get("TIMER")

disconnect_timer = TIMER
roblox_running = False

def get_log_dir():
    system = platform.system()
    if system == "Windows":
        return os.path.expandvars(r"%LOCALAPPDATA%\Roblox\logs")
    elif system == "Darwin":  # macOS
        return os.path.expanduser("~/Library/Logs/Roblox")
    else:  # Assume Linux (Wine)
        # Adjust if you use a custom Wine prefix
        return os.path.expanduser("~/.var/app/org.vinegarhq.Sober/data/sober/sober_logs/")

def get_log_files(log_dir):
    return set(glob.glob(os.path.join(log_dir, "*.log")))

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
            print("[ROBLOX CLOSED] Process ended")
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
        current_logs = get_log_files(log_dir)
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
        while roblox_running:
            if not roblox_running:
                print("Stopping log monitoring because Roblox closed.")
                break

            line = f.readline()
            if disconnect_timer <= 0:
                print("Disconnect timer expired. Closing Roblox...")
                close_roblox()
                break
            if not line:
                time.sleep(1.0)
                disconnect_timer -= 1
                continue
            if DISCONNECTED in line:
                print(f"[DISCONNECT DETECTED] {line.strip()}")

# =============================
# MAIN FUNCTION
# =============================
def main():
    global roblox_running, disconnect_timer

    log_dir = get_log_dir()
    if not os.path.exists(log_dir):
        print(f"Roblox logs folder not found: {log_dir}")
        sys.exit(1)

    existing_logs = get_log_files(log_dir)
    new_log = wait_for_new_log(log_dir, existing_logs)

    roblox_running = True

    # Start process watcher in background
    threading.Thread(target=watch_process, daemon=True).start()

    monitor_log(new_log)

if __name__ == "__main__":
    main()
