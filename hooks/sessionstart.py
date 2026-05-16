#!/usr/bin/env python3
"""
SessionStart hook for cc-discord.

Proactively spawns the discord-daemon when a new Claude Code session begins,
so the first Stop/Notification DM doesn't pay the ~1-15s WSS-handshake cost
on top of its own delay. The daemon is a long-lived process — if one is
already running from an earlier session, this hook is a no-op.

Behavior:
- Daemon already alive  → exit 0, nothing to do.
- Daemon not running    → Popen it detached and exit 0 immediately. We don't
                          wait for READY here; later hooks will block on it
                          as needed.
- Anything goes wrong   → exit 0 silently. Session startup must never be
                          blocked on Discord availability.

Disable with CC_DISCORD_AUTOSTART=off.
"""
import os
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent
CLIENT = PLUGIN_ROOT / "bin" / "discord-client.py"
DAEMON = PLUGIN_ROOT / "bin" / "discord-daemon.py"


def main():
    try:
        sys.stdin.read()
    except Exception:
        pass

    if os.environ.get("CC_DISCORD_AUTOSTART", "on").lower() in ("off", "0", "false", "no"):
        sys.exit(0)

    if os.environ.get("CC_DISCORD_IS_RETRY") == "1":
        sys.exit(0)

    if not CLIENT.exists() or not DAEMON.exists():
        sys.exit(0)

    env = os.environ.copy()
    env["CC_DISCORD_DAEMON"] = str(DAEMON)

    try:
        r = subprocess.run(
            [sys.executable, str(CLIENT), "status"],
            capture_output=True, text=True, timeout=5, env=env,
        )
        if r.returncode == 0:
            sys.exit(0)
    except Exception:
        pass

    log_path = Path.home() / ".claude" / "discord-daemon.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fp = open(log_path, "ab")
    except Exception:
        log_fp = subprocess.DEVNULL

    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_fp,
        "stderr": log_fp,
    }
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
        kwargs["creationflags"] = 0x00000200 | 0x00000008
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen([sys.executable, str(DAEMON)], **kwargs)
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
