#!/usr/bin/env python3
"""
cc-discord notification hook (idle-timer mode).

Fires on Stop, StopFailure, and Notification(permission_prompt|idle_prompt).
Schedules a deferred DM whose delay depends on the event:

    Stop                            → CC_DISCORD_DELAY_STOP        (45s)
    StopFailure                     → CC_DISCORD_DELAY_ERROR       (15s)
    Notification permission_prompt  → CC_DISCORD_DELAY_PERMISSION  (10s)
    Notification idle_prompt        → CC_DISCORD_DELAY_QUESTION    (30s)

PostToolUse and UserPromptSubmit cancel the pending DM if Claude resumes or
the user submits a new prompt inside the window. You only get pinged when
Claude has actually been idle for the full delay.

The DM is keyed by session_id so multiple parallel Claude Code sessions
don't clobber each other's pending notifications.

Always exits 0 so a notification hiccup never blocks Claude Code.
"""
import json
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_DELAYS = {
    "stop":       45.0,
    "error":      15.0,
    "permission": 10.0,
    "question":   30.0,
}

DELAY_ENV_VARS = {
    "stop":       "CC_DISCORD_DELAY_STOP",
    "error":      "CC_DISCORD_DELAY_ERROR",
    "permission": "CC_DISCORD_DELAY_PERMISSION",
    "question":   "CC_DISCORD_DELAY_QUESTION",
}


def safe_exit():
    sys.exit(0)


def _short_cwd(cwd: str) -> str:
    if not cwd:
        return ""
    try:
        p = Path(cwd)
        return f"{p.parent.name}/{p.name}" if p.parent.name else p.name
    except Exception:
        return cwd


def _format_dm(title: str, cwd: str) -> str:
    """One-line title + cwd + mobile link. Markdown-rendered by Discord."""
    parts = [f"## {title}"]
    cwd_short = _short_cwd(cwd)
    if cwd_short:
        parts.append(f"📂 `{cwd_short}`")
    parts.append("🔗 Remote control: <https://claude.ai/code>")
    return "\n".join(parts)


def _delay_for(category: str) -> float:
    raw = os.environ.get(DELAY_ENV_VARS[category], "")
    if raw.strip():
        try:
            v = float(raw)
            return max(0.0, v)
        except ValueError:
            pass
    return DEFAULT_DELAYS[category]


def _classify(event: str, notif_type: str, stopfail_reason: str):
    """Return (category, title) for this hook event, or (None, None) to skip."""
    if event == "Stop":
        return ("stop", "✅ Claude Code finished its turn and is waiting on you")
    if event == "StopFailure":
        reason_titles = {
            "rate_limit":            "rate limited",
            "authentication_failed": "authentication failed",
            "oauth_org_not_allowed": "org not allowed",
            "billing_error":         "billing error",
            "max_output_tokens":     "hit output token limit",
            "invalid_request":       "invalid request",
            "server_error":          "server error",
        }
        readable = reason_titles.get(stopfail_reason, stopfail_reason or "unknown error")
        return ("error", f"🛑 Claude Code stopped — {readable}")
    if event == "Notification":
        if notif_type == "permission_prompt":
            return ("permission", "🔐 Claude needs your approval to run a tool")
        if notif_type == "idle_prompt":
            return ("question", "❓ Claude is asking you a question")
    return (None, None)


def main():
    home = Path.home()
    claude_dir = home / ".claude"
    env_file = Path(os.environ.get("CC_DISCORD_ENV_FILE", claude_dir / ".discord.env"))

    if not env_file.exists():
        hint = claude_dir / "cc-discord.NEEDS_SETUP"
        if not hint.exists():
            try:
                claude_dir.mkdir(parents=True, exist_ok=True)
                hint.write_text(
                    "cc-discord is installed but has no credentials yet.\n"
                    "Run the setup command in Claude Code:\n\n"
                    "    /discord-setup\n\n"
                    "Or remove this file to silence the reminder.\n"
                )
            except Exception:
                pass
        safe_exit()

    try:
        (claude_dir / "cc-discord.NEEDS_SETUP").unlink(missing_ok=True)
    except Exception:
        pass

    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        safe_exit()
    try:
        payload = json.loads(raw)
    except Exception:
        safe_exit()

    event = (payload.get("hook_event_name") or "").strip()
    cwd = payload.get("cwd") or ""
    notif_type = payload.get("notification_type") or ""
    stopfail_reason = payload.get("reason") or ""
    session_id = payload.get("session_id") or ""

    category, title = _classify(event, notif_type, stopfail_reason)
    if category is None:
        safe_exit()

    text = _format_dm(title, cwd)
    delay_f = _delay_for(category)
    key = session_id or "default"

    here = Path(__file__).resolve().parent
    plugin_root = here.parent
    client = plugin_root / "bin" / "discord-client.py"
    daemon = plugin_root / "bin" / "discord-daemon.py"
    if not client.exists():
        safe_exit()

    env = os.environ.copy()
    env["CC_DISCORD_DAEMON"] = str(daemon)

    try:
        subprocess.run(
            [sys.executable, str(client), "defer-notify",
             "--key", key, "--delay", str(delay_f), text],
            input="", capture_output=True, text=True, timeout=20, env=env,
        )
    except Exception:
        pass

    safe_exit()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        safe_exit()
