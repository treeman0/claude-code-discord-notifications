#!/usr/bin/env python3
"""
discord-client: thin CLI that talks to the running discord-daemon over a TCP
loopback socket (127.0.0.1:<port>). Used by the hook script and slash commands.

Discovers the daemon via ~/.claude/discord-daemon.info (port + auth token).
If the daemon isn't running, this script starts it automatically before
sending the request (lazy start). Pass --no-start to disable.

Cross-platform: works on macOS, Linux, and Windows (avoids AF_UNIX which is
not available in CPython on Windows).

Usage:
    discord-client notify "Claude finished"
    discord-client ask "Should I deploy?" --option yes --option no
    discord-client ask "What's the commit message?"
    discord-client status
    discord-client stop
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
INFO_FILE = Path(os.environ.get("CC_DISCORD_INFO", CLAUDE_DIR / "discord-daemon.info"))
PID_FILE = CLAUDE_DIR / "discord-daemon.pid"
DAEMON_SCRIPT_ENV = "CC_DISCORD_DAEMON"


def read_info():
    """Return {port, token, pid, ...} from the info file, or None."""
    if not INFO_FILE.exists():
        return None
    try:
        return json.loads(INFO_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def daemon_alive() -> bool:
    """The info file exists AND the PID it references is running."""
    info = read_info()
    if not info:
        return False
    try:
        os.kill(int(info["pid"]), 0)
    except OSError:
        return False
    except KeyError:
        return False
    return True


def start_daemon(daemon_script: str) -> bool:
    """Spawn the daemon detached. Returns True if it came up within ~15s."""
    if not Path(daemon_script).exists():
        print(f"error: daemon script not found at {daemon_script}", file=sys.stderr)
        return False
    log_path = CLAUDE_DIR / "discord-daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Detach: new process group on Unix, new console flag on Windows.
    kwargs = {"stdin": subprocess.DEVNULL}
    log_fp = open(log_path, "ab")
    kwargs["stdout"] = log_fp
    kwargs["stderr"] = log_fp
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
        kwargs["creationflags"] = 0x00000200 | 0x00000008
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen([sys.executable, daemon_script], **kwargs)

    # Wait for info file to appear, indicating daemon is listening.
    for _ in range(150):  # ~15s
        if INFO_FILE.exists() and read_info() is not None:
            return True
        time.sleep(0.1)
    return False


def send(req: dict, timeout: float = 605.0) -> dict:
    info = read_info()
    if not info:
        raise RuntimeError("daemon info file missing")
    req = dict(req, auth=info["token"])
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(("127.0.0.1", int(info["port"])))
    s.sendall((json.dumps(req) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.decode().strip())


def ensure_daemon(args) -> bool:
    if daemon_alive():
        return True
    if args.no_start:
        print("error: daemon not running and --no-start was set", file=sys.stderr)
        return False
    daemon_script = os.environ.get(DAEMON_SCRIPT_ENV) or \
                    str(Path(__file__).parent / "discord-daemon.py")
    if not start_daemon(daemon_script):
        print("error: daemon failed to start within 15s — check "
              "~/.claude/discord-daemon.log", file=sys.stderr)
        return False
    return True


def cmd_notify(args):
    if not ensure_daemon(args):
        sys.exit(2)
    try:
        resp = send({"cmd": "notify", "text": args.text}, timeout=20)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        sys.exit(1)


def cmd_ask(args):
    if not ensure_daemon(args):
        sys.exit(2)
    req = {"cmd": "ask", "text": args.text,
           "options": args.option or [], "timeout": args.timeout}
    try:
        resp = send(req, timeout=args.timeout + 15)
    except Exception as e:
        print(f"(no reply: {e})", file=sys.stderr)
        sys.exit(1)
    if not resp.get("ok"):
        print(f"(no reply: {resp.get('error', 'unknown')})", file=sys.stderr)
        sys.exit(1)
    print(resp["answer"])


def cmd_status(args):
    if not daemon_alive():
        print("not running")
        sys.exit(1)
    try:
        resp = send({"cmd": "status"}, timeout=5)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        sys.exit(1)
    print(f"running (pid {resp['pid']}, uptime {resp['uptime']}s, "
          f"ready={resp['ready']}, pending={resp['pending']})")


def cmd_stop(args):
    if not daemon_alive():
        print("not running")
        return
    try:
        send({"cmd": "stop"}, timeout=5)
        print("stopped")
    except Exception as e:
        print(f"error stopping: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-start", action="store_true",
                    help="Don't lazy-start the daemon if not running.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_notify = sub.add_parser("notify", help="Send a one-way DM")
    p_notify.add_argument("text")
    p_notify.set_defaults(func=cmd_notify)

    p_ask = sub.add_parser("ask", help="Ask a question and wait for reply")
    p_ask.add_argument("text")
    p_ask.add_argument("--option", action="append",
                       help="An answer option (repeatable). Up to 25.")
    p_ask.add_argument("--timeout", type=float, default=600.0,
                       help="Seconds to wait for reply (default 600)")
    p_ask.set_defaults(func=cmd_ask)

    p_status = sub.add_parser("status", help="Check daemon status")
    p_status.set_defaults(func=cmd_status)

    p_stop = sub.add_parser("stop", help="Stop the daemon")
    p_stop.set_defaults(func=cmd_stop)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
