#!/usr/bin/env python3
"""
PreToolUse hook for cc-discord: relay tool-use permission requests to Discord.

Flow:
    1. Read PreToolUse JSON from stdin (tool_name, tool_input, etc.).
    2. Apply the skip list — read-only tools and safe Bash commands auto-allow.
    3. Otherwise, ask the daemon to DM the user with Approve/Deny buttons.
    4. On approval, print {"permissionDecision":"allow"} JSON, exit 0.
    5. On denial, print {"permissionDecision":"deny", ...} JSON, exit 0.
    6. On any failure (daemon down, Discord unreachable, no reply, timeout),
       print {"permissionDecision":"ask"} JSON, exit 0 — Claude Code falls
       back to its normal in-terminal prompt.

NEVER exit non-zero. NEVER silently allow without an explicit user OK on
Discord. Failing safely means "ask the human at the laptop."

Honored env vars:
    CC_DISCORD_PERMISSION=off       Disable Discord prompts (behave like the
                                    base plugin — every tool gets the normal
                                    Claude Code prompt).
    CC_DISCORD_PERMISSION_TIMEOUT   Seconds to wait for a Discord reply
                                    (default 60).
    CC_DISCORD_PERMISSION_SKIP      Comma-separated tool names that always
                                    auto-allow (default: read-only tools).
    CC_DISCORD_PERMISSION_BASH_SAFE Override the safe-Bash regex.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


# ---- Defaults --------------------------------------------------------------

DEFAULT_SKIP_TOOLS = {
    "Read", "Glob", "Grep", "TodoWrite", "NotebookRead",
    "WebFetch", "WebSearch",
}

# Bash commands considered safe (read-only or test execution). The regex
# matches the start of the command after optional env-var assignments.
DEFAULT_SAFE_BASH_RE = re.compile(
    r"^\s*"
    r"(?:[A-Z_][A-Z0-9_]*=\S+\s+)*"          # leading FOO=bar assignments
    r"(?:"
    r"ls|cat|head|tail|less|more|file|stat|wc|"
    r"grep|find|fd|rg|ack|"
    r"pwd|which|where|whereis|type|hostname|whoami|id|env|date|uptime|"
    r"echo|printf|true|false|"
    r"git\s+(?:status|log|diff|branch|show|remote|config\s+--get|describe|rev-parse|tag|"
    r"ls-files|ls-remote|stash\s+list|reflog\s+show|cherry|cat-file|"
    r"shortlog|whatchanged|fsck|gc\s+--auto|count-objects)\b|"
    r"npm\s+(?:test|run\s+test|run\s+lint|run\s+type-?check|run\s+typecheck|"
    r"ls|ll|list|outdated|view|info|search|--version|-v|run\s+--list)\b|"
    r"pnpm\s+(?:test|run\s+test|run\s+lint|ls|outdated|--version|-v)\b|"
    r"yarn\s+(?:test|run\s+test|run\s+lint|outdated|why|--version|-v)\b|"
    r"pytest|"
    r"python\s+-m\s+pytest|python3\s+-m\s+pytest|"
    r"cargo\s+(?:test|check|clippy|fmt\s+--check|build\s+--dry-run|tree|--version|-V)\b|"
    r"go\s+(?:test|vet|build|version|env|list|doc|fmt\s+-n)\b|"
    r"node\s+--version|python\s+--version|python3\s+--version|"
    r"npm\s+--version|pnpm\s+--version|yarn\s+--version|"
    r"git\s+--version|rustc\s+--version|cargo\s+--version|go\s+version"
    r")\b"
)


# ---- Helpers ---------------------------------------------------------------

def emit(decision: str, reason: str = ""):
    """Print PreToolUse JSON to stdout and exit 0."""
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = reason
    print(json.dumps(out))
    sys.exit(0)


def ask_fallback():
    """Defer to Claude Code's normal permission prompt."""
    emit("ask", "cc-discord couldn't reach Discord; falling back to terminal prompt")


def allow(reason: str = "auto-allowed by cc-discord skip list"):
    emit("allow", reason)


def deny(reason: str):
    emit("deny", reason)


def allow_with_answers(answers: dict, original_questions: list, reason: str = "Answered on Discord"):
    """Emit PreToolUse JSON that approves AskUserQuestion with answers pre-filled.

    Per Claude Code docs: for AskUserQuestion, return permissionDecision='allow'
    together with updatedInput containing the original questions echoed back
    plus an `answers` object mapping each question's text to the chosen label.
    """
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
            "updatedInput": {
                "questions": original_questions,
                "answers": answers,
            },
        }
    }
    print(json.dumps(out))
    sys.exit(0)


def parse_env_skip_list() -> set:
    raw = os.environ.get("CC_DISCORD_PERMISSION_SKIP", "").strip()
    if not raw:
        return set(DEFAULT_SKIP_TOOLS)
    return {tok.strip() for tok in raw.split(",") if tok.strip()}


def safe_bash_re() -> re.Pattern:
    override = os.environ.get("CC_DISCORD_PERMISSION_BASH_SAFE", "").strip()
    if override:
        try:
            return re.compile(override)
        except re.error:
            pass
    return DEFAULT_SAFE_BASH_RE


def is_safe_bash(command: str) -> bool:
    if not command:
        return False
    if not safe_bash_re().match(command):
        return False
    # Even if the leading command is on the safe list, refuse if there are
    # shell-side-effect metacharacters: redirections, pipes, command
    # substitution, or chained commands. These can turn a safe command into
    # something destructive (e.g. `echo x > /etc/passwd`).
    UNSAFE_META = (">", "<", "|", "&", ";", "$(", "`", "&&", "||")
    if any(token in command for token in UNSAFE_META):
        return False
    return True


def truncate(s: str, n: int = 180) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[:n - 1] + "…"


def build_prompt(tool_name: str, tool_input: dict, cwd: str) -> str:
    """Compose a short Discord prompt summarizing the tool call."""
    # Tool-specific summaries make the DM scannable.
    if tool_name == "Bash":
        cmd = truncate(tool_input.get("command", ""), 400)
        desc = truncate(tool_input.get("description", ""), 120)
        body = f"`{cmd}`"
        if desc:
            body += f"\n_{desc}_"
    elif tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or "(unknown path)"
        body = f"📝 `{tool_name}` → `{path}`"
    elif tool_name.startswith("mcp__"):
        # Show just the MCP server + tool name; inputs can be huge.
        body = f"🔌 `{tool_name}`"
    else:
        # Fall back to truncated repr of the input.
        body = f"`{tool_name}`\n`{truncate(json.dumps(tool_input, separators=(',', ':')), 300)}`"

    text = f"**🔐 Tool approval requested: `{tool_name}`**\n{body}"
    if cwd:
        text += f"\n_{cwd}_"
    return text


def ask_discord(text: str, options: list, timeout: float,
                client_path: Path, daemon_path: Path,
                subprocess_timeout: float = None) -> tuple:
    """Ask Discord a question with the given options. Returns (ok, answer).

    ok=False means timeout, daemon error, or unparseable answer — caller should
    fall back. ok=True with the answer string lets caller match against options.
    """
    if subprocess_timeout is None:
        subprocess_timeout = timeout + 10

    env = os.environ.copy()
    env["CC_DISCORD_DAEMON"] = str(daemon_path)

    cmd = [sys.executable, str(client_path), "ask", text, "--timeout", str(timeout)]
    for opt in options:
        cmd.extend(["--option", opt])

    try:
        result = subprocess.run(
            cmd, input="", capture_output=True, text=True,
            timeout=subprocess_timeout, env=env,
        )
    except Exception:
        return (False, None)

    if result.returncode != 0:
        return (False, None)
    return (True, (result.stdout or "").strip())


def handle_ask_user_question(tool_input: dict, cwd: str,
                              client_path: Path, daemon_path: Path,
                              timeout: float):
    """Special handling for the AskUserQuestion tool.

    Send each question to Discord with its actual options as buttons. Collect
    the answers and return them as updatedInput so AskUserQuestion runs with
    pre-filled answers instead of prompting in the terminal.

    On any failure, fall back to terminal prompt (ask).
    """
    questions = tool_input.get("questions") or []
    if not questions:
        # Nothing to ask — let normal flow handle it.
        ask_fallback()
        return

    # Pre-flight intro DM so the user knows what's coming if there are
    # multiple questions. Skip for the single-question case (less noise).
    if len(questions) > 1:
        intro = f"**❓ Claude needs you to answer {len(questions)} question(s)**"
        if cwd:
            intro += f"\n_{cwd}_"
        for i, q in enumerate(questions, 1):
            qtext = (q.get("question") or "").strip()
            intro += f"\n\n**{i}.** {truncate(qtext, 140)}"
        # Best-effort notify. We don't block on this — it's just a heads-up.
        env = os.environ.copy()
        env["CC_DISCORD_DAEMON"] = str(daemon_path)
        try:
            subprocess.run(
                [sys.executable, str(client_path), "notify", intro],
                input="", capture_output=True, text=True, timeout=20, env=env,
            )
        except Exception:
            pass

    # Now ask each question with its options.
    answers = {}
    for q in questions:
        qtext = (q.get("question") or "").strip()
        header = (q.get("header") or "").strip()
        options_in = q.get("options") or []
        multi = bool(q.get("multiSelect"))

        # Build the option labels for buttons (Discord caps at 25 buttons).
        # Each option has {label, description}. Use label as the button text
        # and as the value we match back.
        labels = []
        label_to_option = {}
        for opt in options_in[:25]:
            label = (opt.get("label") or "").strip()
            if not label:
                continue
            labels.append(label)
            label_to_option[label] = opt

        if not labels:
            # Question with no options — fall back to terminal.
            ask_fallback()
            return

        # Build a richer DM body with the descriptions, since buttons only
        # show labels.
        body_lines = [f"**❓ {truncate(qtext, 300)}**"]
        if header:
            body_lines.append(f"_{header}_")
        if multi:
            body_lines.append("_Multi-select: tap one, or reply with comma-separated labels._")
        body_lines.append("")  # blank line before options
        for opt in options_in[:25]:
            label = (opt.get("label") or "").strip()
            desc = (opt.get("description") or "").strip()
            if not label:
                continue
            if desc:
                body_lines.append(f"• **{label}** — {truncate(desc, 120)}")
            else:
                body_lines.append(f"• **{label}**")
        prompt_text = "\n".join(body_lines)

        ok, answer = ask_discord(
            prompt_text, labels, timeout, client_path, daemon_path,
        )
        if not ok or not answer:
            ask_fallback()
            return

        # Match answer to a label. Buttons return the exact label they were
        # built with. Free-text replies are matched case-insensitively against
        # all labels; multi-select free-text comma-separates.
        chosen_label = None
        if multi:
            # Parse comma-separated text or accept a single button label.
            parts = [p.strip() for p in answer.split(",") if p.strip()]
            matched = []
            for part in parts:
                m = _match_label(part, labels)
                if m:
                    matched.append(m)
            if matched:
                # Per AskUserQuestion schema, multi-select expects list-like in
                # the answer string — we'll join with commas.
                chosen_label = ", ".join(matched)
        if chosen_label is None:
            m = _match_label(answer, labels)
            if m:
                chosen_label = m

        if chosen_label is None:
            # Unparseable answer. Bail out to terminal prompt.
            ask_fallback()
            return

        answers[qtext] = chosen_label

    # All questions answered — return as updatedInput.
    allow_with_answers(answers, questions, reason="Answered on Discord")


def _match_label(answer: str, labels: list):
    """Match an answer string back to one of the available labels."""
    a = answer.strip()
    for lbl in labels:
        if a == lbl:
            return lbl
    al = a.lower()
    for lbl in labels:
        if al == lbl.lower():
            return lbl
    # Substring fallback — useful for "approve" matching "✅ Approve".
    for lbl in labels:
        if al in lbl.lower() or lbl.lower() in al:
            return lbl
    return None


# ---- Main ------------------------------------------------------------------

def main():
    # Kill switch: do nothing, defer to normal Claude Code prompt.
    if os.environ.get("CC_DISCORD_PERMISSION", "on").lower() in ("off", "0", "false", "no"):
        sys.exit(0)  # exit silently — Claude Code treats this as "no opinion"

    # Read the hook payload from stdin.
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        sys.exit(0)
    try:
        payload = json.loads(raw)
    except Exception:
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or ""

    # Skip list: read-only tools auto-allow with no DM.
    skip_tools = parse_env_skip_list()
    if tool_name in skip_tools:
        allow(f"{tool_name} is in skip list")

    # Bash-specific safe-list: read-only / test commands auto-allow.
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if is_safe_bash(cmd):
            allow("Safe Bash command")

    # Daemon check before we try to ask.
    env_file = Path(os.environ.get("CC_DISCORD_ENV_FILE",
                                   Path.home() / ".claude" / ".discord.env"))
    if not env_file.exists():
        # cc-discord isn't configured yet. Don't intercept — let normal prompt fire.
        sys.exit(0)

    here = Path(__file__).resolve().parent
    plugin_root = here.parent
    client = plugin_root / "bin" / "discord-client.py"
    daemon = plugin_root / "bin" / "discord-daemon.py"

    if not client.exists():
        # Misinstall; fall back gracefully.
        sys.exit(0)

    timeout = float(os.environ.get("CC_DISCORD_PERMISSION_TIMEOUT", "60"))

    # Special handling for AskUserQuestion: send each question with its real
    # options as buttons, collect the answers, return them as updatedInput.
    if tool_name == "AskUserQuestion":
        # Allow a longer timeout per question for AskUserQuestion since these
        # are real decisions, not yes/no.
        auq_timeout = float(os.environ.get("CC_DISCORD_PERMISSION_TIMEOUT", "180"))
        handle_ask_user_question(tool_input, cwd, client, daemon, auq_timeout)
        return  # not reached — the handler always exits

    text = build_prompt(tool_name, tool_input, cwd)

    # Hard guard: keep the actual subprocess timeout slightly longer than the
    # daemon-side question timeout, so the daemon has a chance to return
    # "timeout" before our subprocess gives up.
    subprocess_timeout = timeout + 10

    env = os.environ.copy()
    env["CC_DISCORD_DAEMON"] = str(daemon)

    try:
        result = subprocess.run(
            [sys.executable, str(client), "ask",
             text,
             "--option", "✅ Approve",
             "--option", "❌ Deny",
             "--timeout", str(timeout)],
            input="",
            capture_output=True,
            text=True,
            timeout=subprocess_timeout,
            env=env,
        )
    except Exception:
        ask_fallback()  # subprocess crashed somehow
        return  # not reached

    if result.returncode != 0:
        # Daemon down, timeout, or generic error — fall back to terminal.
        ask_fallback()
        return

    answer = (result.stdout or "").strip()
    if answer.startswith("✅") or "Approve" in answer:
        allow(f"Approved on Discord")
    elif answer.startswith("❌") or "Deny" in answer:
        deny("Denied on Discord")
    else:
        # User typed something free-form. Be conservative: treat as "ask"
        # rather than auto-allow or auto-deny.
        ask_fallback()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        # Never crash the hook. On any unexpected error, defer to Claude Code.
        sys.exit(0)
