#!/usr/bin/env python3
"""
cc-discord notification hook.

Fires on Stop, Notification (permission_prompt|idle_prompt), and StopFailure.
Reads Claude Code's hook JSON from stdin, builds a short message, sends it to
the daemon over the Unix socket (or named pipe on Windows... see note below).

Credentials live in ~/.claude/.discord.env — outside the plugin cache, so they
survive plugin updates.

Cross-platform: works on macOS, Linux, and Windows (Git Bash, MSYS, native).
Always exits 0 so a notification hiccup never blocks Claude Code.
"""
import json
import os
import sys
import time
from pathlib import Path

# Always exit 0 from this hook, no matter what. A notification problem must
# never disrupt Claude Code itself.
def safe_exit():
    sys.exit(0)


def read_activity_summary(session_id: str, max_lines: int = 200) -> str:
    """Read the per-session activity log and return a compact summary.

    Each line is `timestamp\\ttool_name\\tsummary`. We:
      - Count each tool name and show top-level counts ("3x Bash, 2x Edit")
      - List the first few detail summaries beneath
    """
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

    # Sort tools by count desc, name asc for ties.
    rollup_parts = []
    for tool, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        rollup_parts.append(f"{n}x {tool}")
    rollup = ", ".join(rollup_parts)

    # Show up to 8 details from the back end of the turn (most recent
    # actions are typically the most useful at-a-glance summary).
    detail_lines = []
    for d in details[-8:]:
        d = d.strip()
        if not d:
            continue
        # Trim duplicates ("Bash" by itself can be redundant alongside "Bash: cmd")
        # but keep paths.
        if len(d) > 100:
            d = d[:97] + "…"
        detail_lines.append(f"• {d}")

    text = rollup
    if detail_lines:
        text += "\n" + "\n".join(detail_lines)
    return text


def main():
    home = Path.home()
    claude_dir = home / ".claude"
    env_file = Path(os.environ.get("CC_DISCORD_ENV_FILE", claude_dir / ".discord.env"))

    # First-run: drop a hint file the user will notice, then exit silently.
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

    # Configured: nuke the hint if it's still hanging around.
    try:
        (claude_dir / "cc-discord.NEEDS_SETUP").unlink(missing_ok=True)
    except Exception:
        pass

    # Read payload from stdin.
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
    # stop_hook_active = true means we're being re-invoked from a previous Stop
    # hook's "continue" decision. Don't re-summarize tools in that case.
    stop_hook_active = bool(payload.get("stop_hook_active"))

    # Build title + body per event type.
    title = ""
    body = ""
    if event == "Stop":
        title = "✅ Claude Code finished"
        body = "The agent finished its turn and is idle."
        if not stop_hook_active and session_id:
            summary = read_activity_summary(session_id)
            if summary:
                body += f"\n\n**Tools used this turn:**\n{summary}"
            # Truncate or delete the file so next turn starts clean.
            try:
                log_path = Path.home() / ".claude" / "cc-discord" / f"activity-{session_id}.log"
                if log_path.exists():
                    log_path.unlink()
            except Exception:
                pass
    elif event == "Notification":
        if notif_type == "permission_prompt":
            title = "🔐 Claude Code wants permission"
            body = msg or "Claude is asking to use a tool."
        elif notif_type == "idle_prompt":
            title = "❓ Claude Code is waiting on you"
            body = msg or "Claude is asking a question."
        else:
            title = "🔔 Claude Code notification"
            body = msg or "(no message)"
    elif event == "StopFailure":
        reason_titles = {
            "rate_limit":            "⏳ Claude Code: rate limited",
            "authentication_failed": "🔑 Claude Code: auth failed",
            "oauth_org_not_allowed": "🔑 Claude Code: org not allowed",
            "billing_error":         "💳 Claude Code: billing error",
            "max_output_tokens":     "📏 Claude Code: hit output token limit",
            "invalid_request":       "⚠️ Claude Code: invalid request",
            "server_error":          "🛑 Claude Code: server error",
        }
        title = reason_titles.get(stopfail_reason, "🛑 Claude Code stopped with an error")
        body = (f"Reason: {stopfail_reason or 'unknown'}. "
                "The turn ended early — restart or fix the underlying issue.")
    else:
        title = f"🔔 Claude Code: {event or 'event'}"
        body = msg or "(no message)"

    # Cap body at 800 chars (Discord allows 2000, but notifications stay scannable).
    if len(body) > 800:
        body = body[:797] + "..."

    text = f"**{title}**\n{body}"
    if os.environ.get("CC_DISCORD_INCLUDE_DIR", "1") != "0" and cwd:
        text += f"\n_{cwd}_"

    # Find sibling scripts in this plugin's bin/.
    here = Path(__file__).resolve().parent
    plugin_root = here.parent
    client = plugin_root / "bin" / "discord-client.py"
    daemon = plugin_root / "bin" / "discord-daemon.py"

    if not client.exists():
        # Misinstall; nothing we can do silently.
        safe_exit()

    # Run the client. Inherit our Python interpreter — that's the one with
    # websockets, presumably. Fire and forget; suppress all output, never block
    # Claude Code.
    import subprocess
    env = os.environ.copy()
    env["CC_DISCORD_DAEMON"] = str(daemon)

    try:
        # Short overall timeout — the daemon should answer fast. If it has to
        # cold-start, that takes a few seconds but the client waits for it.
        result = subprocess.run(
            [sys.executable, str(client), "notify", text],
            input="",
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
        # On error, append to log (debug only, opt-in).
        if result.returncode != 0 and os.environ.get("CC_DISCORD_DEBUG", "0") == "1":
            log = claude_dir / "cc-discord.log"
            try:
                with log.open("a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%F %T')}] client rc={result.returncode}: "
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
        # Never let any error escape this hook.
        safe_exit()
