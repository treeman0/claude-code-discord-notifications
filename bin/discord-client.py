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

# Replies may contain emoji (e.g. "✅ Approve", "❌ Deny"). On Windows, stdout
# defaults to cp1252 and crashes with UnicodeEncodeError when we print them.
# Force UTF-8 so the answer round-trips cleanly back to the hook.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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


def _pid_running(pid: int) -> bool:
    """Cross-platform check whether `pid` belongs to a running process.

    On Unix, os.kill(pid, 0) signals nothing and either succeeds or raises.
    On Windows, os.kill(pid, 0) actually delivers CTRL_C, which is wrong for
    a liveness check — and on MSYS Python it can lie outright. Use OpenProcess
    on Windows via ctypes for an accurate read.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if not handle:
                # Could be access denied or "no such process". The most common
                # case is "no such process" → treat as not running.
                err = ctypes.get_last_error()
                # ERROR_INVALID_PARAMETER (87) is what you get for a missing PID.
                # ERROR_ACCESS_DENIED (5) means the process EXISTS but we can't
                # query it — treat as running to be safe.
                return err == 5
            # Process opened — check exit code to confirm it's still active.
            STILL_ACTIVE = 259
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                running = (exit_code.value == STILL_ACTIVE)
            else:
                running = True  # opened but couldn't read — assume running
            kernel32.CloseHandle(handle)
            return running
        except Exception:
            return False
    # POSIX
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def daemon_alive() -> bool:
    """The info file exists AND the PID it references is running AND the
    socket port it claims is actually accepting connections.

    All three checks matter on Windows because:
    - Info file can be stale (old daemon died without cleanup)
    - PID can be reused by an unrelated process
    - The daemon might be midway through shutdown
    """
    info = read_info()
    if not info:
        return False
    pid = info.get("pid")
    if not pid or not _pid_running(int(pid)):
        return False
    # Verify the port is open. If it isn't, the info file is stale.
    port = info.get("port")
    if not port:
        return False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(("127.0.0.1", int(port)))
        s.close()
        return True
    except (OSError, socket.timeout):
        return False


def start_daemon(daemon_script: str) -> bool:
    """Spawn the daemon detached and wait until it's connected to Discord.

    "Connected" means the gateway READY event has fired — not just that the
    local socket is listening. If we returned earlier, the next ask/notify
    would race the gateway handshake and fall back to the terminal whenever
    READY took longer than the daemon's _wait_ready timeout.
    """
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

    # Stage 1: wait up to 15s for the info file (local socket listening).
    for _ in range(150):
        if INFO_FILE.exists() and read_info() is not None:
            break
        time.sleep(0.1)
    else:
        return False

    # Stage 2: poll status until the gateway is READY. WSS handshake + IDENTIFY
    # + READY is usually <1s but can take noticeably longer on cold Windows
    # boots and slow networks — budget another 15s here.
    for _ in range(150):
        try:
            resp = send({"cmd": "status"}, timeout=2)
            if resp.get("ok") and resp.get("ready"):
                return True
        except Exception:
            pass
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


def cmd_defer_notify(args):
    if not ensure_daemon(args):
        sys.exit(2)
    try:
        resp = send({
            "cmd": "defer_notify",
            "key": args.key,
            "text": args.text,
            "delay": args.delay,
        }, timeout=20)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        sys.exit(1)


def cmd_cancel_deferred(args):
    if not ensure_daemon(args):
        sys.exit(2)
    try:
        resp = send({"cmd": "cancel_deferred", "key": args.key or ""}, timeout=20)
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

    p_defer = sub.add_parser("defer-notify",
                              help="Schedule a DM N seconds out, keyed by session_id")
    p_defer.add_argument("--key", required=True,
                          help="Cancellation key (e.g. session_id)")
    p_defer.add_argument("--delay", type=float, default=30.0,
                          help="Seconds to wait before sending (default 30)")
    p_defer.add_argument("text")
    p_defer.set_defaults(func=cmd_defer_notify)

    p_cancel = sub.add_parser("cancel-deferred",
                               help="Cancel a pending deferred DM. Omit --key to cancel all.")
    p_cancel.add_argument("--key", default="", help="Specific key to cancel; default cancels all")
    p_cancel.set_defaults(func=cmd_cancel_deferred)

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
