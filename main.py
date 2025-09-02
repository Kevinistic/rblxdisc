import json
import os
import sys
import time
import glob
import platform

# ==============================
# LOAD CONFIG
# ==============================
with open("config.json", "r") as f:
    config = json.load(f)

DISCONNECTED = config.get("DISCONNECTED")
CLOSED = config.get("CLOSED")
USER_ID = config.get("USER_ID")
TIMER = config.get("TIMER")

def get_log_dir():
    system = platform.system()
    if system == "Windows":
        return os.path.expandvars(r"%LOCALAPPDATA%\Roblox\logs")
    elif system == "Darwin":  # macOS
        return os.path.expanduser("~/Library/Logs/Roblox")
    else:  # Assume Linux (Wine)
        # Adjust if you use a custom Wine prefix
        return os.path.expanduser("~/.wine/drive_c/users/$USER/Local Settings/Application Data/Roblox/logs")

def get_log_files(log_dir):
    return set(glob.glob(os.path.join(log_dir, "*.log")))

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
    print(f"Monitoring: {log_file}")
    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.2)
                continue
            if DISCONNECTED in line or CLOSED in line:
                print(f"[DISCONNECT DETECTED] {line.strip()}")

def main():
    log_dir = get_log_dir()
    if not os.path.exists(log_dir):
        print(f"Roblox logs folder not found: {log_dir}")
        sys.exit(1)

    existing_logs = get_log_files(log_dir)
    new_log = wait_for_new_log(log_dir, existing_logs)
    monitor_log(new_log)

if __name__ == "__main__":
    main()
