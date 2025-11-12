#!/usr/bin/env python3
"""
RblxDisc Wrapper - Watchdog Process Manager for single_user_bot.py

This wrapper manages the lifecycle of the main bot process, providing:
- Automatic restart on crash with exponential backoff
- Graceful shutdown handling (Ctrl+C, SIGTERM)
- Clean process management
- Process state monitoring

Usage:
    python wrapper.py                    # Run with defaults
    python wrapper.py --max-restarts 10  # Max 10 restart attempts
    python wrapper.py --no-auto-restart  # Disable auto-restart on crash
"""

import os
import sys
import time
import subprocess
import signal
import argparse
import logging
from datetime import datetime
from pathlib import Path

# =========================
# CONFIGURATION
# =========================
SCRIPT_DIR = Path(__file__).parent
BOT_SCRIPT = SCRIPT_DIR / "single_user_bot.py"
LOG_DIR = SCRIPT_DIR / "logs"
WRAPPER_LOG_FILE = LOG_DIR / "wrapper.log"

# Defaults
DEFAULT_MAX_RESTARTS = 50
DEFAULT_RESTART_DELAY = 10  # seconds
DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT = 5  # seconds

# =========================
# LOGGING SETUP
# =========================
LOG_DIR.mkdir(exist_ok=True)

class WrapperLogger:
    """Simple logger for wrapper process."""
    
    def __init__(self, log_file):
        self.log_file = log_file
    
    def _write(self, level, message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] [{level}] {message}"
        print(log_line)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(log_line + "\n")
        except Exception as e:
            print(f"[ERROR] Failed to write to wrapper log: {e}")
    
    def info(self, message):
        self._write("INFO", message)
    
    def warn(self, message):
        self._write("WARN", message)
    
    def error(self, message):
        self._write("ERROR", message)
    
    def debug(self, message):
        self._write("DEBUG", message)

logger = WrapperLogger(WRAPPER_LOG_FILE)

# =========================
# WRAPPER STATE
# =========================
class WrapperState:
    def __init__(self, max_restarts, auto_restart, restart_delay):
        self.max_restarts = max_restarts
        self.auto_restart = auto_restart
        self.restart_delay = restart_delay
        self.restart_count = 0
        self.process = None
        self.should_exit = False
        self.crash_count_last_minute = 0
        self.last_crash_times = []
    
    def record_crash(self):
        """Track crash for rate limiting."""
        now = time.time()
        # Remove crashes older than 1 minute
        self.last_crash_times = [t for t in self.last_crash_times if now - t < 60]
        self.last_crash_times.append(now)
        self.crash_count_last_minute = len(self.last_crash_times)
    
    def should_rate_limit(self):
        """Check if too many crashes in last minute."""
        return self.crash_count_last_minute > 5
    
    def can_restart(self):
        """Check if we can attempt another restart."""
        return self.restart_count < self.max_restarts and self.auto_restart

state = None

# =========================
# SIGNAL HANDLERS
# =========================
def handle_sigterm(signum, frame):
    """Handle SIGTERM: graceful shutdown."""
    logger.info("Received SIGTERM. Shutting down gracefully...")
    state.should_exit = True
    if state.process and state.process.poll() is None:
        try:
            state.process.terminate()
            try:
                state.process.wait(timeout=DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT)
                logger.info("Bot process terminated cleanly.")
            except subprocess.TimeoutExpired:
                logger.warn("Bot process did not terminate within timeout, forcing kill...")
                state.process.kill()
                state.process.wait()
        except Exception as e:
            logger.error(f"Error during graceful shutdown: {e}")
    sys.exit(0)

def handle_sigint(signum, frame):
    """Handle SIGINT (Ctrl+C): graceful shutdown."""
    logger.info("Received SIGINT. Shutting down gracefully...")
    state.should_exit = True
    if state.process and state.process.poll() is None:
        try:
            state.process.terminate()
            try:
                state.process.wait(timeout=DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT)
                logger.info("Bot process terminated cleanly.")
            except subprocess.TimeoutExpired:
                logger.warn("Bot process did not terminate within timeout, forcing kill...")
                state.process.kill()
                state.process.wait()
        except Exception as e:
            logger.error(f"Error during graceful shutdown: {e}")
    sys.exit(0)

# =========================
# PROCESS MANAGEMENT
# =========================
def start_bot_process():
    """Start the main bot process."""
    try:
        logger.info(f"Starting bot process: {BOT_SCRIPT}")
        process = subprocess.Popen(
            [sys.executable, str(BOT_SCRIPT)],
            cwd=str(SCRIPT_DIR),
            stdout=None,
            stderr=None,
        )
        logger.info(f"Bot process started with PID {process.pid}")
        return process
    except Exception as e:
        logger.error(f"Failed to start bot process: {e}")
        return None

def monitor_process(process):
    """Monitor the bot process and wait for it to exit."""
    try:
        return_code = process.wait()
        logger.info(f"Bot process exited with code {return_code}")
        return return_code
    except Exception as e:
        logger.error(f"Error monitoring process: {e}")
        return -1

# =========================
# MAIN WRAPPER LOOP
# =========================
def run_wrapper(args):
    """Main wrapper loop with restart logic."""
    global state
    state = WrapperState(
        max_restarts=args.max_restarts,
        auto_restart=args.auto_restart,
        restart_delay=args.restart_delay,
    )
    
    logger.info("=" * 70)
    logger.info("RblxDisc Wrapper Started")
    logger.info(f"Auto-restart: {state.auto_restart}")
    logger.info(f"Max restarts: {state.max_restarts}")
    logger.info(f"Restart delay: {state.restart_delay}s")
    logger.info("=" * 70)
    
    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigint)
    if hasattr(signal, "SIGBREAK"):  # Windows
        signal.signal(signal.SIGBREAK, handle_sigterm)
    
    restart_delay = state.restart_delay
    
    while not state.should_exit:
        # Optionally update the repository before starting the bot
        if args.auto_update:
            try:
                git_root = find_git_root(SCRIPT_DIR)
                if git_root:
                    logger.info(f"Checking for updates in git repository: {git_root}")
                    updated = git_fetch_and_pull(git_root, args.git_branch, logger)
                    if updated:
                        logger.info("Repository updated (git pull successful).")
                    else:
                        logger.debug("No updates found or pull not needed.")
                else:
                    logger.warn("Git repository root not found; skipping auto-update.")
            except Exception as e:
                logger.error(f"Auto-update failed: {e}")

        # Start the bot process
        state.process = start_bot_process()
        if not state.process:
            logger.error("Failed to start bot process, exiting wrapper.")
            sys.exit(1)
        
        # Wait for process to exit
        return_code = monitor_process(state.process)
        state.process = None
        
        # Check exit reason
        if return_code == 0:
            logger.info("Bot exited cleanly (return code 0). Stopping wrapper.")
            break
        
        if return_code == 1:
            logger.warn("Bot exited with code 1 (likely configuration error). Stopping wrapper.")
            break
        
        # Process crashed
        state.record_crash()
        state.restart_count += 1
        
        logger.warn(f"Bot process crashed (return code {return_code})")
        logger.warn(f"Crash count in last minute: {state.crash_count_last_minute}")
        
        # Check rate limiting
        if state.should_rate_limit():
            logger.error("Too many crashes in the last minute (5+). Stopping wrapper to prevent infinite restart loop.")
            break
        
        # Check if we can restart
        if not state.can_restart():
            logger.error(f"Reached max restart limit ({state.max_restarts}). Stopping wrapper.")
            break
        
        # Calculate backoff delay
        backoff = min(restart_delay * (state.restart_count // 5), 120)  # Cap at 2 minutes
        logger.warn(f"Restarting bot in {backoff}s (attempt {state.restart_count}/{state.max_restarts})...")
        time.sleep(backoff)
    
    logger.info("Wrapper shutdown complete.")

# =========================
# ARGUMENT PARSING
# =========================
def parse_args():
    parser = argparse.ArgumentParser(
        description="RblxDisc Wrapper - Watchdog for single_user_bot.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python wrapper.py                       # Run with defaults
  python wrapper.py --max-restarts 10     # Maximum 10 restart attempts
  python wrapper.py --no-auto-restart     # Run once, don't auto-restart on crash
  python wrapper.py --restart-delay 30    # Wait 30s between restarts
        """
    )
    
    parser.add_argument(
        "--max-restarts",
        type=int,
        default=DEFAULT_MAX_RESTARTS,
        help=f"Maximum number of restart attempts (default: {DEFAULT_MAX_RESTARTS})",
    )
    
    parser.add_argument(
        "--no-auto-restart",
        action="store_false",
        dest="auto_restart",
        help="Disable automatic restart on crash (run once)",
    )
    
    parser.add_argument(
        "--restart-delay",
        type=int,
        default=DEFAULT_RESTART_DELAY,
        help=f"Initial delay between restarts in seconds (default: {DEFAULT_RESTART_DELAY}s)",
    )
    
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 1.0.0",
    )
    parser.add_argument(
        "--auto-update",
        action="store_true",
        dest="auto_update",
        help="Automatically fetch & pull updates from git before starting the bot",
    )
    parser.add_argument(
        "--git-branch",
        type=str,
        default=None,
        help="Git branch to track (defaults to the current branch detected in the repo)",
    )
    
    return parser.parse_args()


def find_git_root(start_path: Path):
    """Walk upwards from start_path to find the .git directory. Return Path or None."""
    p = start_path.resolve()
    while True:
        if (p / ".git").exists():
            return p
        if p.parent == p:
            return None
        p = p.parent


def git_fetch_and_pull(repo_path: Path, branch: str | None, logger: WrapperLogger) -> bool:
    """Fetch remote and pull if upstream has new commits.

    Returns True if a pull (update) was performed, False otherwise.
    """
    repo_dir = str(repo_path)
    # Determine current branch if not provided
    try:
        p = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir, capture_output=True, text=True)
        if p.returncode != 0:
            logger.warn(f"Unable to determine git branch: {p.stderr.strip()}")
            return False
        current_branch = p.stdout.strip()
    except Exception as e:
        logger.error(f"git rev-parse failed: {e}")
        return False

    branch_to_use = branch or current_branch
    # Fetch remote updates
    logger.info(f"Fetching remote for branch '{branch_to_use}'...")
    fetch = subprocess.run(["git", "fetch", "--quiet"], cwd=repo_dir, capture_output=True, text=True)
    if fetch.returncode != 0:
        logger.warn(f"git fetch failed: {fetch.stderr.strip()}")
        # Not fatal; continue to try comparing

    # Get local HEAD
    local = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True)
    if local.returncode != 0:
        logger.warn(f"git rev-parse HEAD failed: {local.stderr.strip()}")
        return False
    local_hash = local.stdout.strip()

    # Get remote upstream hash
    upstream = subprocess.run(["git", "rev-parse", "@{u}"], cwd=repo_dir, capture_output=True, text=True)
    if upstream.returncode != 0:
        logger.debug("No upstream configured or fetch failed; skipping auto-update.")
        return False
    upstream_hash = upstream.stdout.strip()

    if local_hash == upstream_hash:
        logger.debug("Local is up-to-date with upstream.")
        return False

    # Pull (fast-forward only)
    logger.info("Updates detected; attempting git pull --ff-only...")
    pull = subprocess.run(["git", "pull", "--ff-only"], cwd=repo_dir, capture_output=True, text=True)
    if pull.returncode == 0:
        logger.info(f"git pull succeeded: {pull.stdout.strip()}")
        return True
    else:
        logger.error(f"git pull failed: {pull.stderr.strip()} (stdout: {pull.stdout.strip()})")
        return False

# =========================
# ENTRY POINT
# =========================
if __name__ == "__main__":
    if not BOT_SCRIPT.exists():
        print(f"[ERROR] Bot script not found: {BOT_SCRIPT}")
        sys.exit(1)
    
    args = parse_args()
    
    try:
        run_wrapper(args)
    except KeyboardInterrupt:
        logger.info("Wrapper interrupted by user.")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Wrapper crashed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)
