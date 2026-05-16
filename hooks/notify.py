#!/usr/bin/env python3
"""
cc-discord notification hook (idle-timer mode).

Fires on Stop, Notification(permission_prompt|idle_prompt), and StopFailure.
Instead of sending a DM immediately, schedules a deferred DM N seconds out
(default 30, override with CC_DISCORD_IDLE_DELAY). PostToolUse and
UserPromptSubmit hooks cancel the deferred DM if the user gets back to work
inside the window, so you only get pinged when Claude is genuinely stuck/idle.

The DM is keyed by session_id so multiple parallel Claude Code sessions don't
clobber each other's pending notifications.

Credentials live in ~/.claude/.discord.env. Always exits 0 so a notification
hiccup never blocks Claude Code.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path


# Always exit 0 from this hook, no matter what. A notification problem must
# never disrupt Claude Code itself.
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


def read_activity_summary(session_id: str, max_lines: int = 200) -> str:
    """Read the per-session activity log and return a compact summary."""
    log_path = Path.home() / ".claude" / "cc-discord" / f"activity-{session_id}.log"
    if not log_path.exists():
        return ""
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()[-max_lines:]
    except Exception:
        return ""
    if not lines:
        return ""

    counts: dict = {}
    details: list = []
    for line in lines:
        parts = line.split("\t", 2)
        if len(parts) < 2:
            continue
        tool = parts[1]
        summary = parts[2] if len(parts) > 2 else tool
        counts[tool] = counts.get(tool, 0) + 1
        details.append(summary)

    if not counts:
        return ""

    rollup_parts = []
    for tool, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        rollup_parts.append(f"{n}× {tool}")
    rollup = ", ".join(rollup_parts)

    detail_lines = []
    for d in details[-8:]:
        d = d.strip()
        if not d:
            continue
        if len(d) > 100:
            d = d[:97] + "…"
        detail_lines.append(f"• {d}")

    text = rollup
    if detail_lines:
        text += "\n" + "\n".join(detail_lines)
    return text


def _format_dm(reason: str, title: str, detail: str, cwd: str) -> str:
    """Build a pretty Discord DM body. Discord supports Markdown."""
    parts = [f"## {title}"]
    if detail:
        parts.append(detail.strip())
    cwd_short = _short_cwd(cwd)
    if cwd_short:
        parts.append(f"📂 `{cwd_short}`")
    parts.append("")  # blank line before link
    parts.append("🔗 Pick up on mobile: <https://claude.ai/code>")
    body = "\n".join(parts)
    # Discord hard cap is 2000 chars; keep the whole DM well under.
    if len(body) > 1800:
        body = body[:1797] + "..."
    return body


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
    msg = payload.get("message") or ""
    notif_type = payload.get("notification_type") or ""
    stopfail_reason = payload.get("reason") or ""
    session_id = payload.get("session_id") or ""
    stop_hook_active = bool(payload.get("stop_hook_active"))

    # Key the deferred notification by session_id so independent sessions don't
    # clobber each other. Fall back to "default" if no session_id present.
    key = session_id or "default"

    title = ""
    detail = ""
    if event == "Stop":
        title = "✅ Claude Code finished its turn"
        detail = "Agent went idle after completing the task."
        if not stop_hook_active and session_id:
            summary = read_activity_summary(session_id)
            if summary:
                detail += f"\n\n**Tools used:**\n{summary}"
            try:
                log_path = Path.home() / ".claude" / "cc-discord" / f"activity-{session_id}.log"
                if log_path.exists():
                    log_path.unlink()
            except Exception:
                pass
    elif event == "Notification":
        if notif_type in ("permission_prompt", "idle_prompt"):
            title = "❓ Claude Code is waiting on you"
            detail = msg or "It's blocked on input — a question, a tool approval, or something it needs you to look at."
        else:
            title = "🔔 Claude Code notification"
            detail = msg or "(no message)"
    elif event == "StopFailure":
        reason_titles = {
            "rate_limit":            "⏳ Rate limited",
            "authentication_failed": "🔑 Auth failed",
            "oauth_org_not_allowed": "🔑 Org not allowed",
            "billing_error":         "💳 Billing error",
            "max_output_tokens":     "📏 Hit output token limit",
            "invalid_request":       "⚠️ Invalid request",
            "server_error":          "🛑 Server error",
        }
        title = "🛑 Claude Code stopped with an error: " + reason_titles.get(
            stopfail_reason, stopfail_reason or "unknown")
        detail = (
            f"Reason: `{stopfail_reason or 'unknown'}`. The turn ended early — "
            "restart or fix the underlying issue."
        )
    else:
        title = f"🔔 Claude Code: {event or 'event'}"
        detail = msg or "(no message)"

    text = _format_dm(notif_type or event, title, detail, cwd)

    delay = os.environ.get("CC_DISCORD_IDLE_DELAY", "30")
    try:
        delay_f = float(delay)
    except ValueError:
        delay_f = 30.0
    if delay_f < 0:
        delay_f = 0.0

    here = Path(__file__).resolve().parent
    plugin_root = here.parent
    client = plugin_root / "bin" / "discord-client.py"
    daemon = plugin_root / "bin" / "discord-daemon.py"
    if not client.exists():
        safe_exit()

    env = os.environ.copy()
    env["CC_DISCORD_DAEMON"] = str(daemon)

    try:
        result = subprocess.run(
            [sys.executable, str(client), "defer-notify",
             "--key", key, "--delay", str(delay_f), text],
            input="",
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
        if result.returncode != 0 and os.environ.get("CC_DISCORD_DEBUG", "0") == "1":
            log = claude_dir / "cc-discord.log"
            try:
                with log.open("a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%F %T')}] defer-notify rc={result.returncode}: "
                            f"stderr={result.stderr.strip()[:300]}\n")
            except Exception:
                pass
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
