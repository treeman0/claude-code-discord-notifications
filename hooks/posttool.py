#!/usr/bin/env python3
"""
PostToolUse hook for cc-discord: silently accumulate tool-call summaries in a
per-session file. The Stop hook reads this file at end of turn to build the
"finished. Tools used: ..." summary in the Stop DM.

This hook is intentionally fast and silent — no DMs, no daemon round-trips.
Just append a line to ~/.claude/cc-discord/activity-<session_id>.log.

We don't aggregate counts here; we just record one line per tool call.
The Stop hook does the grouping when it reads the file.

Always exits 0. Any failure is silent.
"""
import json
import os
import sys
import time
from pathlib import Path


def main():
    if os.environ.get("CC_DISCORD_TICKER", "on").lower() in ("off", "0", "false", "no"):
        sys.exit(0)

    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        sys.exit(0)
    try:
        payload = json.loads(raw)
    except Exception:
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or "unknown"

    if not tool_name:
        sys.exit(0)

    # Skip a couple of high-volume tools that bloat the summary without
    # adding signal: TodoWrite fires after every plan step.
    if tool_name in ("TodoWrite",):
        sys.exit(0)

    # Build a short summary that's useful at end-of-turn.
    summary = tool_name
    if tool_name == "Bash":
        cmd = (tool_input.get("command") or "").strip().split("\n", 1)[0]
        if cmd:
            summary = f"Bash: {cmd[:80]}"
    elif tool_name in ("Edit", "Write", "MultiEdit"):
        path = tool_input.get("file_path", "")
        if path:
            summary = f"{tool_name}: {path}"
    elif tool_name in ("Read", "NotebookRead"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path", "")
        if path:
            summary = f"{tool_name}: {path}"

    log_dir = Path.home() / ".claude" / "cc-discord"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"activity-{session_id}.log"

    line = f"{int(time.time())}\t{tool_name}\t{summary}\n"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # never block on logging failure

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)
