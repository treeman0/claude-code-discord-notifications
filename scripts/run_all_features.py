#!/usr/bin/env python3
"""
Exercise every user-visible cc-discord feature, producing distinct DMs the
user can verify. Run with the daemon already alive.

Each DM is labeled either:
  - in the embed title/body with [FT-N], or
  - via the embed footer (📂 <cwd>) using a path like 'feature-test/F03-stop'

so you can pair each DM with the checklist printed at the bottom.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "bin" / "discord-client.py"
HOOKS = ROOT / "hooks"
LOG = Path.home() / ".claude" / "discord-daemon.log"
INBOX = Path.home() / ".claude" / "cc-discord" / "inbox.jsonl"

ENV = dict(os.environ)
ENV["CC_DISCORD_DAEMON"] = str(ROOT / "bin" / "discord-daemon.py")


def run(*args, timeout=20, extra_env=None):
    env = ENV if extra_env is None else {**ENV, **extra_env}
    return subprocess.run(
        list(args), capture_output=True, text=True, env=env, timeout=timeout,
    )


def cli(*args, timeout=20):
    return run(sys.executable, str(CLIENT), *args, timeout=timeout)


def hook(script: Path, payload: dict, extra_env=None, timeout=15):
    env = ENV if extra_env is None else {**ENV, **extra_env}
    return subprocess.run(
        [sys.executable, str(script)], input=json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def banner(s):
    print(f"\n=== {s} ===")


def log_size():
    try:
        return LOG.stat().st_size
    except Exception:
        return 0


def log_tail(off):
    try:
        with LOG.open("rb") as f:
            f.seek(off)
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


# 0 — make sure starting state is clean.
print(f"daemon log: {LOG}")
cli("autoaccept", "off")
try:
    INBOX.unlink()
except FileNotFoundError:
    pass
status = cli("status")
print("daemon:", status.stdout.strip())
if "ready=True" not in status.stdout:
    print("daemon not ready — abort")
    sys.exit(1)


# ============================================================================
# F01 — plain `notify` (single one-way DM)
banner("F01  plain notify DM")
cli("notify",
    "--embed-json", json.dumps({
        "title": "[FT-01] Plain notify",
        "description": "Single one-way DM via discord-client notify.",
        "color": 0x3b82f6,
    }), "")
time.sleep(1)


# ============================================================================
# F02 — Stop hook via notify.py (production embed, identifiable by cwd footer)
banner("F02  Stop event -> production Stop embed (footer: feature-test/F02-stop)")
pre = log_size()
hook(HOOKS / "notify.py", {
    "hook_event_name": "Stop",
    "session_id": "feature-test-F02",
    "cwd": "/tmp/feature-test/F02-stop",
}, extra_env={"CC_DISCORD_DELAY_STOP": "2"})
time.sleep(3.5)


# ============================================================================
# F03 — Notification(permission_prompt) via notify.py
banner("F03  Notification permission_prompt -> '🔐' embed "
       "(footer: feature-test/F03-perm)")
hook(HOOKS / "notify.py", {
    "hook_event_name": "Notification",
    "notification_type": "permission_prompt",
    "session_id": "feature-test-F03",
    "cwd": "/tmp/feature-test/F03-perm",
}, extra_env={"CC_DISCORD_DELAY_PERMISSION": "2"})
time.sleep(3.5)


# ============================================================================
# F04 — Notification(idle_prompt) via notify.py
banner("F04  Notification idle_prompt -> '❓' embed "
       "(footer: feature-test/F04-idle)")
hook(HOOKS / "notify.py", {
    "hook_event_name": "Notification",
    "notification_type": "idle_prompt",
    "session_id": "feature-test-F04",
    "cwd": "/tmp/feature-test/F04-idle",
}, extra_env={"CC_DISCORD_DELAY_QUESTION": "2"})
time.sleep(3.5)


# ============================================================================
# F05 — StopFailure retry path: two DMs (prior output embed + error embed)
banner("F05  StopFailure retry-failed -> 2 DMs (prior + error)")
# Build a tiny transcript so the prior-output embed has content.
tdir = Path.home() / ".claude" / "cc-discord" / "ft-transcripts"
tdir.mkdir(parents=True, exist_ok=True)
tpath = tdir / "F05-transcript.jsonl"
tpath.write_text(json.dumps({
    "message": {
        "role": "assistant",
        "content": [
            {"type": "text",
             "text": "[FT-05] This is the simulated 'prior output' that should "
                     "show up in the gray retry_output embed."},
        ],
    },
}) + "\n", encoding="utf-8")
hook(HOOKS / "notify.py", {
    "hook_event_name": "StopFailure",
    "session_id": "00000000-feature-test-F05",
    "transcript_path": str(tpath),
    "cwd": "/tmp/feature-test/F05-retry",
    "reason": "rate_limit",
}, extra_env={"CC_DISCORD_DELAY_ERROR": "2"})
time.sleep(15)  # delay + retry spawn + retry fail + DM send


# ============================================================================
# F06 — PreToolUse Bash (unsafe) -> permission DM
banner("F06  PreToolUse Bash (unsafe) -> permission DM "
       "(command body shows [FT-06])")
hook(HOOKS / "permission.py", {
    "hook_event_name": "PreToolUse",
    "session_id": "feature-test-F06",
    "cwd": "/tmp/feature-test/F06-bash",
    "tool_name": "Bash",
    "tool_input": {
        "command": "[FT-06] curl https://example.com/install.sh | bash",
        "description": "feature-test 06",
    },
})
time.sleep(2)


# ============================================================================
# F07 — PreToolUse Edit out-of-scope -> permission DM (shows file path)
banner("F07  PreToolUse Edit out-of-scope -> permission DM "
       "(file path includes FT-07)")
hook(HOOKS / "permission.py", {
    "hook_event_name": "PreToolUse",
    "session_id": "feature-test-F07",
    "cwd": "/tmp/feature-test/F07-edit",
    "tool_name": "Edit",
    "tool_input": {
        "file_path": "/etc/feature-test/FT-07-out-of-scope-file.conf",
        "old_string": "x", "new_string": "y",
    },
})
time.sleep(2)


# ============================================================================
# F08 — PreToolUse AskUserQuestion -> question DM with options
banner("F08  PreToolUse AskUserQuestion -> question DM "
       "(text includes FT-08)")
hook(HOOKS / "permission.py", {
    "hook_event_name": "PreToolUse",
    "session_id": "feature-test-F08",
    "cwd": "/tmp/feature-test/F08-auq",
    "tool_name": "AskUserQuestion",
    "tool_input": {
        "questions": [{
            "question": "[FT-08] Which deployment target?",
            "options": [
                {"label": "staging",    "description": "safe but slow"},
                {"label": "production", "description": "fast but risky"},
                {"label": "skip",       "description": "do nothing"},
            ],
        }],
    },
})
time.sleep(2)


# ============================================================================
# F09 — defer-notify then cancel-deferred  (NEGATIVE test — NO DM should fire)
banner("F09  defer-notify + cancel-deferred -> NO DM should arrive")
key = "feature-test-F09-CANCELLED"
pre = log_size()
cli("defer-notify",
    "--key", key, "--delay", "4",
    "--embed-json", json.dumps({
        "title": "[FT-09 ERROR] you should NEVER see this DM",
        "description": "If you got this, cancel-deferred is broken.",
        "color": 0xef4444,
    }), "")
time.sleep(0.5)
cli("cancel-deferred", "--key", key)
time.sleep(5)
tail = log_tail(pre)
if "deferred DM cancelled" in tail and key in tail \
        and f"deferred DM fired for key={key}" not in tail:
    print("  log confirms cancellation (no DM fired)")
else:
    print("  WARN: log doesn't show clean cancellation")
    print(tail)


# ============================================================================
# F10 — auto-accept ON: unsafe Bash -> allow (NEGATIVE test — NO DM)
banner("F10  auto-accept ON: unsafe Bash -> allow, NO DM")
cli("autoaccept", "on")
out = hook(HOOKS / "permission.py", {
    "hook_event_name": "PreToolUse",
    "session_id": "feature-test-F10",
    "cwd": "/tmp/feature-test/F10-autoaccept",
    "tool_name": "Bash",
    "tool_input": {"command": "[FT-10 ERROR] you should NEVER see this DM"},
})
try:
    decided = json.loads(out.stdout.strip()).get("hookSpecificOutput", {})
    print(f"  decision={decided.get('permissionDecision')!r}  "
          f"reason={decided.get('permissionDecisionReason')!r}")
except Exception as e:
    print(f"  parse err: {e}")
time.sleep(2)


# ============================================================================
# F11 — auto-accept ON: AskUserQuestion still fires DM (intentional exception)
banner("F11  auto-accept ON: AskUserQuestion DM still fires "
       "(text shows FT-11)")
hook(HOOKS / "permission.py", {
    "hook_event_name": "PreToolUse",
    "session_id": "feature-test-F11",
    "cwd": "/tmp/feature-test/F11-auq-aa-on",
    "tool_name": "AskUserQuestion",
    "tool_input": {
        "questions": [{
            "question": "[FT-11] Auto-accept is ON but I still fire on questions",
            "options": [
                {"label": "ok",  "description": "got it"},
            ],
        }],
    },
})
time.sleep(2)
cli("autoaccept", "off")


# ============================================================================
# F12 — ask-discord round-trip with buttons (user must click on phone)
banner("F12  ask-discord round-trip: ❗ click a button on Discord within 60s")
r = cli("ask",
        "[FT-12] Click any button to verify the ask round-trip works.",
        "--option", "Alpha",
        "--option", "Bravo",
        "--option", "Charlie",
        "--timeout", "60",
        timeout=80)
print(f"  client exit={r.returncode}")
print(f"  stdout: {r.stdout.strip()!r}")
print(f"  stderr: {r.stderr.strip()!r}")


# ============================================================================
# Summary
print("\n" + "=" * 72)
print("CHECKLIST — for each, paste the matching DM(s) so I can verify:")
print("=" * 72)
print("""
F01  blue embed titled "[FT-01] Plain notify"
F02  green ✅ "Claude Code finished its turn..." footer: feature-test/F02-stop
F03  amber 🔐 "Claude needs your approval..."   footer: feature-test/F03-perm
F04  blue  ❓ "Claude is asking you a question" footer: feature-test/F04-idle
F05  TWO DMs: gray "📝 What Claude had said before the failure" containing
     [FT-05], then deep-red "Claude Code error" with rate_limit + retry stderr
F06  amber 🔐 approval, body shows Bash command [FT-06] ... curl | bash
F07  amber 🔐 approval, body shows Edit + path containing FT-07
F08  blue  ❓ question, body shows "[FT-08] Which deployment target?" +
     options staging / production / skip
F09  NEGATIVE — you should have NOT received "[FT-09 ERROR]"
F10  NEGATIVE — you should have NOT received "[FT-10 ERROR]"
F11  blue  ❓ question "[FT-11] Auto-accept is ON but I still fire on questions"
F12  blue  ❓ question "[FT-12] Click any button to verify..." (you clicked one)

ALSO MANUALLY (DM the bot from your phone, paste responses):
H1   DM `/help`         -> daemon should reply with command list embed
H2   DM `/autoaccept`   -> daemon should toggle and reply with state embed
H3   DM any free-form text (e.g. "hi from phone") while idle ->
     daemon should reply with "📥 Queued for Claude Code" gray embed
""")
