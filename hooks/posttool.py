#!/usr/bin/env python3
"""
PostToolUse hook for cc-discord.

Single job: cancel any pending deferred idle DM for this session. A tool
just ran, so Claude is still working — we don't want the 30s-idle ping
to fire mid-task.

Always exits 0. Any failure is silent.
"""
import json
import os
import subprocess
import sys
from pathlib import Path


def _cancel_deferred(session_id: str):
    if not session_id:
        return
    here = Path(__file__).resolve().parent
    plugin_root = here.parent
    client = plugin_root / "bin" / "discord-client.py"
    daemon = plugin_root / "bin" / "discord-daemon.py"
    if not client.exists():
        return
    env = os.environ.copy()
    env["CC_DISCORD_DAEMON"] = str(daemon)
    try:
        subprocess.run(
            [sys.executable, str(client), "--no-start", "cancel-deferred",
             "--key", session_id],
            input="", capture_output=True, text=True, timeout=5, env=env,
        )
    except Exception:
        pass


def main():
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        sys.exit(0)
    try:
        payload = json.loads(raw)
    except Exception:
        sys.exit(0)

    _cancel_deferred(payload.get("session_id") or "")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)
