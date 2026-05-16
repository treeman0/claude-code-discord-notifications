#!/usr/bin/env python3
"""
UserPromptSubmit hook for cc-discord.

While Claude Code is idle (or even while running), any free-form DM the user
sends from their phone is appended to ~/.claude/cc-discord/inbox.jsonl by the
daemon. When the user then submits any prompt in the terminal, this hook
drains the inbox and prepends those messages as additionalContext so Claude
processes them alongside the new prompt.

Behavior:
- Empty inbox → exit 0 silently, prompt is unchanged.
- One or more queued items → emit additionalContext, then clear the file.
- Any error → exit 0 silently rather than blocking the user's prompt.

The hook is intentionally fast and forgiving: a failure here must never stop
the user from working.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


CLAUDE_DIR = Path.home() / ".claude"
INBOX_FILE = CLAUDE_DIR / "cc-discord" / "inbox.jsonl"


def safe_exit():
    sys.exit(0)


def _cancel_deferred(session_id: str):
    """Best-effort: tell the daemon to drop any pending idle DM for this
    session. A new user prompt means the user is back at the keyboard."""
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
    if os.environ.get("CC_DISCORD_INBOX", "on").lower() in ("off", "0", "false", "no"):
        safe_exit()

    # Consume stdin so the harness doesn't see a closed pipe before we
    # respond. Also extract session_id so we can cancel any pending DM.
    session_id = ""
    try:
        raw_in = sys.stdin.read()
        try:
            session_id = (json.loads(raw_in) or {}).get("session_id") or ""
        except Exception:
            pass
    except Exception:
        pass

    _cancel_deferred(session_id)

    if not INBOX_FILE.exists():
        safe_exit()

    try:
        raw = INBOX_FILE.read_text(encoding="utf-8")
    except Exception:
        safe_exit()

    items = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except Exception:
            # Tolerate malformed lines — treat as plain text.
            items.append({"ts": time.time(), "content": line})

    # Clear the file as soon as we've parsed it. Even if we fail to emit
    # context, we don't want the same items resurfacing every prompt.
    try:
        INBOX_FILE.unlink()
    except Exception:
        pass

    items = [it for it in items if (it.get("content") or "").strip()]
    if not items:
        safe_exit()

    lines = [
        "[cc-discord inbox]",
        f"You have {len(items)} message(s) sent from Discord while idle. "
        "Treat each as a directive from the user, taken together with the "
        "current prompt below.",
        "",
    ]
    for it in items:
        when = ""
        ts = it.get("ts")
        if isinstance(ts, (int, float)):
            try:
                when = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            except Exception:
                when = ""
        content = (it.get("content") or "").strip()
        # Discord caps DMs at 2000 chars; we don't need an extra cap here,
        # but trim the obviously absurd ones so the prompt stays focused.
        if len(content) > 4000:
            content = content[:4000] + "…"
        if when:
            lines.append(f"- ({when}) {content}")
        else:
            lines.append(f"- {content}")

    additional = "\n".join(lines)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional,
        }
    }
    print(json.dumps(out))
    safe_exit()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Belt-and-suspenders: never block the user's prompt on our error.
        sys.exit(0)
