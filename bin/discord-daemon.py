#!/usr/bin/env python3
"""
discord-daemon: long-running process that owns a Discord gateway WebSocket
connection, sends DMs, and waits for replies from your phone.

CLIs talk to it over a TCP loopback socket (127.0.0.1:<random_port>). The
daemon writes its port + auth token to ~/.claude/discord-daemon.info on
startup. The client reads that file, opens the connection, and proves identity
by including the token in each request.

Protocol (one JSON object per line, newline-terminated):
    request:  {"auth": "...", "cmd": "notify"|"ask"|"status"|"stop", ...}
    response: {"ok": true|false, "answer": "...", "error": "...", ...}

Loopback + a per-startup token gives us cross-platform IPC (Windows-friendly,
since AF_UNIX isn't reliable on Python/Windows), local-only access (bind is
127.0.0.1), and proof that we reached our own daemon.

Requires: Python 3.8+, the `websockets` package.
"""

import asyncio
import json
import logging
import os
import secrets
import signal
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

try:
    import websockets
except ImportError:
    sys.stderr.write(
        "error: the 'websockets' package is required. Install with:\n"
        "    python3 -m pip install --user websockets\n"
        "or re-run /discord-setup which installs it for you.\n"
    )
    sys.exit(2)


# ---- Config ------------------------------------------------------------------

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
ENV_FILE = Path(os.environ.get("CC_DISCORD_ENV_FILE", CLAUDE_DIR / ".discord.env"))
INFO_FILE = Path(os.environ.get("CC_DISCORD_INFO", CLAUDE_DIR / "discord-daemon.info"))
PID_FILE = CLAUDE_DIR / "discord-daemon.pid"
LOG_FILE = CLAUDE_DIR / "discord-daemon.log"

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
API_BASE = "https://discord.com/api/v10"
DM_CHANNEL_CACHE: dict = {}

# Intents: DIRECT_MESSAGES (1<<12). DM message content arrives without
# MESSAGE_CONTENT, and button clicks arrive as INTERACTION_CREATE which needs
# no intent at all.
INTENTS = 1 << 12

CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("discord-daemon")


def load_env() -> dict:
    if not ENV_FILE.exists():
        log.error("no env file at %s", ENV_FILE)
        sys.exit(2)
    out = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        out[k.strip()] = v
    return out


# ---- REST helpers -----------------------------------------------------------

def _api(method: str, path: str, token: str, body: Optional[dict] = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "ClaudeCodeDiscordNotifier (https://github.com/treeman0/claude-code-discord-notifications, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode()
        except Exception:
            err_body = ""
        raise RuntimeError(f"Discord API {method} {path}: HTTP {e.code} {err_body}") from None


def open_dm(token: str, user_id: str) -> str:
    if user_id in DM_CHANNEL_CACHE:
        return DM_CHANNEL_CACHE[user_id]
    ch = _api("POST", "/users/@me/channels", token, {"recipient_id": user_id})
    DM_CHANNEL_CACHE[user_id] = ch["id"]
    return ch["id"]


def send_message(token: str, channel_id: str, content: str,
                 components: Optional[list] = None) -> dict:
    body = {"content": content}
    if components:
        body["components"] = components
    return _api("POST", f"/channels/{channel_id}/messages", token, body)


def ack_interaction(interaction_id: str, interaction_token: str,
                    edited_content: str) -> None:
    """ACK a button click. Type 7 = UPDATE_MESSAGE — edits the original message."""
    url = f"{API_BASE}/interactions/{interaction_id}/{interaction_token}/callback"
    body = {"type": 7, "data": {"content": edited_content, "components": []}}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
    except Exception as e:
        log.warning("interaction ack failed: %s", e)


# ---- Pending question registry ---------------------------------------------

class Pending:
    def __init__(self):
        self._by_id: dict = {}
        self._by_msg: dict = {}

    def add(self, request_id, message_id, future, options):
        self._by_id[request_id] = {
            "message_id": message_id, "future": future,
            "options": options, "created": time.time(),
        }
        self._by_msg[message_id] = request_id

    def resolve_by_message(self, message_id, answer) -> bool:
        req_id = self._by_msg.get(message_id)
        if not req_id:
            return False
        return self._resolve(req_id, answer)

    def resolve_oldest(self, answer) -> bool:
        if not self._by_id:
            return False
        req_id = min(self._by_id, key=lambda k: self._by_id[k]["created"])
        return self._resolve(req_id, answer)

    def _resolve(self, req_id, answer) -> bool:
        entry = self._by_id.pop(req_id, None)
        if not entry:
            return False
        self._by_msg.pop(entry["message_id"], None)
        fut = entry["future"]
        if not fut.done():
            fut.get_loop().call_soon_threadsafe(fut.set_result, {"answer": answer})
        return True

    def cancel(self, req_id):
        entry = self._by_id.pop(req_id, None)
        if entry:
            self._by_msg.pop(entry["message_id"], None)

    def count(self) -> int:
        return len(self._by_id)


# ---- Daemon state ----------------------------------------------------------

class Daemon:
    def __init__(self, env: dict):
        self.token = env["DISCORD_BOT_TOKEN"]
        self.user_id = env["DISCORD_USER_ID"]
        self.pending = Pending()
        self.ws = None
        self.seq = None
        self.session_id = None
        self.resume_url = None
        self.ready_event = asyncio.Event()
        self.started_at = time.time()

    async def run_gateway(self):
        backoff = 1
        while True:
            url = self.resume_url or GATEWAY_URL
            log.info("connecting to gateway %s (resume=%s)", url, bool(self.resume_url))
            try:
                async with websockets.connect(url, max_size=2**20) as ws:
                    self.ws = ws
                    backoff = 1
                    await self._gateway_session(ws)
            except Exception as e:
                log.warning("gateway error: %s — reconnecting in %ds", e, backoff)
                self.resume_url = None
                self.session_id = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _gateway_session(self, ws):
        hello = json.loads(await ws.recv())
        if hello.get("op") != 10:
            raise RuntimeError(f"expected Hello, got op={hello.get('op')}")
        hb_ms = hello["d"]["heartbeat_interval"]
        log.info("hello received, hb=%dms", hb_ms)
        hb_task = asyncio.create_task(self._heartbeat_loop(ws, hb_ms))

        if self.session_id and self.seq is not None:
            await ws.send(json.dumps({
                "op": 6,
                "d": {"token": self.token, "session_id": self.session_id, "seq": self.seq},
            }))
        else:
            await ws.send(json.dumps({
                "op": 2,
                "d": {
                    "token": self.token, "intents": INTENTS,
                    "properties": {"os": sys.platform, "browser": "cc-discord",
                                   "device": "cc-discord"},
                },
            }))

        try:
            async for msg in ws:
                await self._handle_event(json.loads(msg))
        finally:
            hb_task.cancel()

    async def _heartbeat_loop(self, ws, interval_ms: int):
        jitter = secrets.randbelow(1000) / 1000.0
        await asyncio.sleep(interval_ms / 1000.0 * jitter)
        while True:
            try:
                await ws.send(json.dumps({"op": 1, "d": self.seq}))
            except Exception:
                return
            await asyncio.sleep(interval_ms / 1000.0)

    async def _handle_event(self, p: dict):
        op = p.get("op")
        if op == 0:
            if p.get("s") is not None:
                self.seq = p["s"]
            t = p.get("t")
            d = p.get("d") or {}
            if t == "READY":
                self.session_id = d.get("session_id")
                self.resume_url = d.get("resume_gateway_url")
                log.info("READY: logged in as %s", (d.get("user") or {}).get("username"))
                self.ready_event.set()
            elif t == "RESUMED":
                log.info("session resumed")
                self.ready_event.set()
            elif t == "MESSAGE_CREATE":
                await self._on_message(d)
            elif t == "INTERACTION_CREATE":
                await self._on_interaction(d)
        elif op == 1:
            await self.ws.send(json.dumps({"op": 1, "d": self.seq}))
        elif op == 7:
            log.info("server asked to reconnect")
            await self.ws.close(code=4000)
        elif op == 9:
            log.warning("invalid session — re-identifying")
            self.session_id = None
            self.resume_url = None
            await asyncio.sleep(2)
            await self.ws.close(code=4000)
        elif op == 11:
            pass

    async def _on_message(self, d: dict):
        author = d.get("author") or {}
        if author.get("bot"):
            return
        if author.get("id") != self.user_id:
            return
        content = (d.get("content") or "").strip()
        if not content:
            return
        log.info("DM reply received: %r", content[:80])
        if not self.pending.resolve_oldest(content):
            log.info("(no pending question to match this reply to)")

    async def _on_interaction(self, d: dict):
        if d.get("type") != 3:
            return
        user_obj = d.get("user") or (d.get("member") or {}).get("user") or {}
        if user_obj.get("id") != self.user_id:
            return
        message_id = (d.get("message") or {}).get("id")
        custom_id = (d.get("data") or {}).get("custom_id", "")
        answer = custom_id.split(":", 1)[1] if custom_id.startswith("ans:") else custom_id
        log.info("button click for message %s: %r", message_id, answer)
        edited = f"❓ ~~(answered)~~\n→ **{answer}**"
        asyncio.create_task(asyncio.to_thread(
            ack_interaction, d["id"], d["token"], edited
        ))
        self.pending.resolve_by_message(message_id, answer)

    async def do_notify(self, text: str) -> dict:
        await self._wait_ready(timeout=10)
        channel = await asyncio.to_thread(open_dm, self.token, self.user_id)
        await asyncio.to_thread(send_message, self.token, channel, text)
        return {"ok": True}

    async def do_ask(self, text: str, options: list, timeout: float) -> dict:
        await self._wait_ready(timeout=10)
        channel = await asyncio.to_thread(open_dm, self.token, self.user_id)
        components = self._build_components(options) if options else None
        msg = await asyncio.to_thread(send_message, self.token, channel, text, components)
        message_id = msg["id"]

        request_id = secrets.token_hex(8)
        future = asyncio.get_running_loop().create_future()
        self.pending.add(request_id, message_id, future, options)
        log.info("ask: posted message %s, awaiting reply (timeout=%ss)", message_id, timeout)

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return {"ok": True, "answer": result["answer"]}
        except asyncio.TimeoutError:
            self.pending.cancel(request_id)
            return {"ok": False, "error": "timeout",
                    "message": f"No reply received within {int(timeout)}s."}

    @staticmethod
    def _build_components(options: list) -> list:
        options = options[:25]
        rows = []
        for i in range(0, len(options), 5):
            chunk = options[i:i+5]
            rows.append({
                "type": 1,
                "components": [{
                    "type": 2, "style": 2,
                    "label": opt[:80], "custom_id": f"ans:{opt}"[:100],
                } for opt in chunk],
            })
        return rows

    async def _wait_ready(self, timeout: float):
        try:
            await asyncio.wait_for(self.ready_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("daemon not ready — gateway connection not established yet")


# ---- TCP loopback server ---------------------------------------------------

async def serve_tcp(daemon: Daemon, auth_token: str, stop_event: asyncio.Event):
    INFO_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def handle(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=300)
            if not line:
                return
            try:
                req = json.loads(line)
            except Exception as e:
                writer.write(json.dumps({"ok": False, "error": f"bad json: {e}"}).encode() + b"\n")
                await writer.drain()
                return

            given = req.get("auth", "")
            if not secrets.compare_digest(given, auth_token):
                writer.write(json.dumps({"ok": False, "error": "auth"}).encode() + b"\n")
                await writer.drain()
                return

            cmd = req.get("cmd")
            try:
                if cmd == "notify":
                    resp = await daemon.do_notify(req.get("text", ""))
                elif cmd == "ask":
                    resp = await daemon.do_ask(
                        req.get("text", ""),
                        req.get("options") or [],
                        float(req.get("timeout") or 600),
                    )
                elif cmd == "status":
                    resp = {
                        "ok": True,
                        "ready": daemon.ready_event.is_set(),
                        "pending": daemon.pending.count(),
                        "uptime": int(time.time() - daemon.started_at),
                        "pid": os.getpid(),
                    }
                elif cmd == "stop":
                    resp = {"ok": True}
                    writer.write(json.dumps(resp).encode() + b"\n")
                    await writer.drain()
                    stop_event.set()
                    return
                else:
                    resp = {"ok": False, "error": f"unknown cmd: {cmd}"}
            except Exception as e:
                log.exception("error handling cmd=%s", cmd)
                resp = {"ok": False, "error": str(e)}

            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    # Bind 127.0.0.1, kernel-chosen port.
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    info_payload = json.dumps({
        "port": port, "token": auth_token, "pid": os.getpid(),
        "started_at": daemon.started_at, "version": 1,
    })
    INFO_FILE.write_text(info_payload, encoding="utf-8")
    try:
        os.chmod(INFO_FILE, 0o600)
    except Exception:
        pass  # Windows: chmod is a noop; OK
    log.info("listening on 127.0.0.1:%d (info=%s)", port, INFO_FILE)

    async with server:
        await stop_event.wait()
        log.info("stop requested")


# ---- Entry point ------------------------------------------------------------

def write_pid():
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def cleanup():
    for p in (INFO_FILE, PID_FILE):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


async def main():
    env = load_env()
    if "DISCORD_BOT_TOKEN" not in env or "DISCORD_USER_ID" not in env:
        log.error("env file missing DISCORD_BOT_TOKEN or DISCORD_USER_ID")
        sys.exit(2)

    write_pid()
    daemon = Daemon(env)
    stop_event = asyncio.Event()
    auth_token = secrets.token_urlsafe(24)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, ValueError):
            pass  # Windows

    gateway_task = asyncio.create_task(daemon.run_gateway())
    server_task = asyncio.create_task(serve_tcp(daemon, auth_token, stop_event))

    try:
        await stop_event.wait()
    finally:
        gateway_task.cancel()
        server_task.cancel()
        for t in (gateway_task, server_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        cleanup()
        log.info("daemon stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
