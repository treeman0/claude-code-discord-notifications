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


# ---- SSL setup (Windows-friendly) -------------------------------------------
# Python on Windows often doesn't trust system CA roots out of the box, which
# breaks gateway TLS with CERTIFICATE_VERIFY_FAILED. Try, in order:
#   1. SSL_CERT_FILE env var (if user set it explicitly)
#   2. certifi.where() (if certifi is installed — the standard Python CA bundle)
#   3. truststore (Python 3.10+ stdlib hook into the OS cert store, if available)
#   4. default ssl context (works on macOS/Linux out of the box)
import ssl
def build_ssl_context():
    # 1. Explicit override
    cafile = os.environ.get("SSL_CERT_FILE")
    if cafile and os.path.exists(cafile):
        return ssl.create_default_context(cafile=cafile)
    # 2. certifi (pip install certifi)
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    # 3. truststore (Python 3.10+, hooks into OS cert store)
    try:
        import truststore
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        return ctx
    except ImportError:
        pass
    # 4. Default — works on macOS/Linux, may fail on Windows.
    return ssl.create_default_context()

SSL_CONTEXT = build_ssl_context()


# ---- Config ------------------------------------------------------------------

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
ENV_FILE = Path(os.environ.get("CC_DISCORD_ENV_FILE", CLAUDE_DIR / ".discord.env"))
INFO_FILE = Path(os.environ.get("CC_DISCORD_INFO", CLAUDE_DIR / "discord-daemon.info"))
PID_FILE = CLAUDE_DIR / "discord-daemon.pid"
LOG_FILE = CLAUDE_DIR / "discord-daemon.log"
INBOX_FILE = CLAUDE_DIR / "cc-discord" / "inbox.jsonl"

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

# Discord sits behind Cloudflare, which 403s ("error code: 1010") any request
# whose User-Agent it doesn't recognise — including Python's default
# "Python-urllib/3.x". Every outbound request to discord.com must set this.
USER_AGENT = (
    "DiscordBot (https://github.com/treeman0/claude-code-discord-notifications, 1.0)"
)


def _api(method: str, path: str, token: str, body: Optional[dict] = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        # Only pass context for https URLs (tests use http://).
        if req.full_url.startswith("https://"):
            with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        else:
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


def edit_message(token: str, channel_id: str, message_id: str,
                 content: str, remove_components: bool = True) -> None:
    """Edit a previously-sent message; optionally strip its components.

    Used to clean up DM ask messages when the answer arrived elsewhere
    (terminal prompt, timeout). Removing components prevents the user from
    tapping a stale button and getting an 'interaction failed' error.

    Best-effort. Logs and continues on failure.
    """
    body = {"content": content}
    if remove_components:
        body["components"] = []
    try:
        _api("PATCH", f"/channels/{channel_id}/messages/{message_id}", token, body)
    except Exception as e:
        log.warning("edit_message failed for msg %s: %s", message_id, e)


def ack_interaction_deferred(interaction_id: str, interaction_token: str) -> None:
    """Immediately ACK a button click with type 6 (DEFERRED_UPDATE_MESSAGE).

    This is the critical path. Discord gives us 3 seconds from the moment the
    user taps the button to acknowledge the interaction, or it shows "This
    interaction failed" to the user. type 6 just says "I got it, the UI will
    update in a moment" and is almost always sub-100ms. We then do the actual
    content edit in a separate PATCH below — that has a 15-minute window.
    """
    url = f"{API_BASE}/interactions/{interaction_id}/{interaction_token}/callback"
    body = {"type": 6}  # DEFERRED_UPDATE_MESSAGE
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        if req.full_url.startswith("https://"):
            with urllib.request.urlopen(req, timeout=3, context=SSL_CONTEXT) as r:
                r.read()
        else:
            with urllib.request.urlopen(req, timeout=3) as r:
                r.read()
    except Exception as e:
        log.warning("interaction ack failed: %s", e)


def edit_original_interaction_response(application_id: str, interaction_token: str,
                                       new_content: str) -> None:
    """Edit the original message after a deferred ACK.

    Uses the interaction webhook PATCH endpoint. Token is valid 15 minutes.
    No 3-second deadline; safe to do at any pace.
    """
    url = f"{API_BASE}/webhooks/{application_id}/{interaction_token}/messages/@original"
    body = {"content": new_content, "components": []}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="PATCH",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        if req.full_url.startswith("https://"):
            with urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT) as r:
                r.read()
        else:
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
    except Exception as e:
        log.warning("interaction edit failed: %s", e)


def ack_interaction(interaction_id: str, interaction_token: str,
                    edited_content: str) -> None:
    """Legacy single-shot ACK (type 7 UPDATE_MESSAGE). Kept for compatibility.

    Prefer ack_interaction_deferred + edit_original_interaction_response for
    button presses, since the combined version is at risk of breaching the
    3-second interaction deadline.
    """
    url = f"{API_BASE}/interactions/{interaction_id}/{interaction_token}/callback"
    body = {"type": 7, "data": {"content": edited_content, "components": []}}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        if req.full_url.startswith("https://"):
            with urllib.request.urlopen(req, timeout=5, context=SSL_CONTEXT) as r:
                r.read()
        else:
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
    except Exception as e:
        log.warning("interaction ack failed: %s", e)


# ---- Pending question registry ---------------------------------------------

class Pending:
    def __init__(self):
        self._by_id: dict = {}
        self._by_msg: dict = {}

    def add(self, request_id, message_id, future, options,
            channel_id=None, original_text=None):
        self._by_id[request_id] = {
            "message_id": message_id, "future": future,
            "options": options, "created": time.time(),
            "channel_id": channel_id,
            "original_text": original_text,
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

    def get_entry(self, message_id):
        """Return the pending entry for a message_id without resolving it."""
        req_id = self._by_msg.get(message_id)
        if not req_id:
            return None
        return self._by_id.get(req_id)

    def get_oldest_entry(self):
        if not self._by_id:
            return None
        req_id = min(self._by_id, key=lambda k: self._by_id[k]["created"])
        return self._by_id.get(req_id)

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
        """Remove a pending entry without resolving its future."""
        entry = self._by_id.pop(req_id, None)
        if entry:
            self._by_msg.pop(entry["message_id"], None)
            return entry
        return None

    def count(self) -> int:
        return len(self._by_id)


# ---- Daemon state ----------------------------------------------------------

class Daemon:
    def __init__(self, env: dict):
        self.token = env["DISCORD_BOT_TOKEN"]
        self.user_id = env["DISCORD_USER_ID"]
        self.bot_user_id = ""  # set from READY event; used as application_id
        self.pending = Pending()
        self.ws = None
        self.seq = None
        self.session_id = None
        self.resume_url = None
        self.ready_event = asyncio.Event()
        self.started_at = time.time()
        # Deferred notifications: session_id -> asyncio.Task. A new hook fires
        # `defer_notify` to schedule a DM N seconds out. PostToolUse and
        # UserPromptSubmit fire `cancel_deferred` if the user gets back to work
        # before the timer elapses, suppressing the DM.
        self._deferred: dict = {}

    async def run_gateway(self):
        backoff = 1
        while True:
            url = self.resume_url or GATEWAY_URL
            log.info("connecting to gateway %s (resume=%s)", url, bool(self.resume_url))
            try:
                connect_kwargs = {"max_size": 2**20}
                if url.startswith("wss://"):
                    connect_kwargs["ssl"] = SSL_CONTEXT
                async with websockets.connect(url, **connect_kwargs) as ws:
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
                # Capture the bot's own user ID — same as application ID for
                # interaction webhook URLs.
                self.bot_user_id = (d.get("user") or {}).get("id", "")
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

        # Grab the oldest pending entry (the one this reply will resolve)
        # BEFORE resolving, so we still have its message_id/channel_id.
        entry = self.pending.get_oldest_entry()

        if not self.pending.resolve_oldest(content):
            # No pending question — treat the DM as a queued command for
            # Claude Code's next user prompt. The UserPromptSubmit hook
            # (hooks/inbox.py) drains this file on the next prompt and
            # injects the items as additionalContext.
            try:
                INBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
                with INBOX_FILE.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(
                        {"ts": time.time(), "content": content}
                    ) + "\n")
                log.info("queued DM to inbox (%d chars)", len(content))
                # Best-effort ack reaction would be nice, but adding a
                # reaction requires READ_MESSAGE_HISTORY + READ_REACTIONS
                # intents we don't currently request. Acknowledge inline
                # so the user sees their message landed.
                try:
                    channel_id = d.get("channel_id")
                    if channel_id:
                        ack = (
                            "📥 Queued for Claude Code "
                            f"(will be picked up on your next prompt)."
                        )
                        await asyncio.to_thread(
                            send_message, self.token, channel_id, ack,
                        )
                except Exception as e:
                    log.warning("inbox ack send failed: %s", e)
            except Exception as e:
                log.warning("inbox write failed: %s", e)
            return

        # Edit the original DM message to show the answer + drop buttons,
        # so it doesn't sit there with live buttons that go nowhere.
        if entry and entry.get("channel_id") and entry.get("message_id"):
            original = entry.get("original_text") or "(question)"
            new_text = f"❓ {original}\n💬 **Replied: {content[:200]}**"
            asyncio.create_task(asyncio.to_thread(
                edit_message, self.token,
                entry["channel_id"], entry["message_id"],
                new_text, True,
            ))

    async def _on_interaction(self, d: dict):
        if d.get("type") != 3:
            return
        user_obj = d.get("user") or (d.get("member") or {}).get("user") or {}
        if user_obj.get("id") != self.user_id:
            return
        message_id = (d.get("message") or {}).get("id")
        custom_id = (d.get("data") or {}).get("custom_id", "")
        answer = custom_id.split(":", 1)[1] if custom_id.startswith("ans:") else custom_id
        interaction_id = d["id"]
        interaction_token = d["token"]
        log.info("button click for message %s: %r", message_id, answer)

        # CRITICAL: ACK within 3 seconds, or Discord shows "interaction failed".
        # We send the lightweight type-6 DEFERRED_UPDATE_MESSAGE here. This is
        # almost always sub-100ms. Doing it synchronously (with await) means
        # any future event handling waits behind it, but the ACK itself is so
        # fast that this is the right tradeoff vs risking a missed deadline.
        try:
            await asyncio.wait_for(
                asyncio.to_thread(ack_interaction_deferred,
                                  interaction_id, interaction_token),
                timeout=2.5,  # below Discord's 3s deadline
            )
        except asyncio.TimeoutError:
            log.warning("ack timed out, message may have shown 'interaction failed'")

        # Resolve the pending future right away — Claude needs the answer.
        # Capture the entry first so we still have original_text for the edit.
        entry = self.pending.get_entry(message_id)
        self.pending.resolve_by_message(message_id, answer)

        # Now edit the original message asynchronously (no time pressure).
        # We use the interaction webhook PATCH endpoint, which is valid for
        # 15 minutes regardless of how long the ACK took.
        original = (entry or {}).get("original_text") or "(question)"
        edited = f"❓ {original}\n✅ **You tapped: {answer}**"
        application_id = self.bot_user_id  # populated from READY event
        if application_id:
            asyncio.create_task(asyncio.to_thread(
                edit_original_interaction_response,
                application_id, interaction_token, edited,
            ))
        else:
            log.warning("bot_user_id not set; skipping interaction edit")

    async def do_notify(self, text: str) -> dict:
        await self._wait_ready(timeout=30)
        channel = await asyncio.to_thread(open_dm, self.token, self.user_id)
        await asyncio.to_thread(send_message, self.token, channel, text)
        return {"ok": True}

    async def do_defer_notify(self, key: str, text: str, delay: float) -> dict:
        """Schedule a DM for `delay` seconds from now, keyed by `key`.

        If another defer with the same key arrives, the previous timer is
        cancelled and replaced (most recent reason wins). cancel_deferred
        cancels any timer for that key. The actual DM fires only if the
        timer reaches zero without being cancelled.
        """
        if not key:
            return {"ok": False, "error": "missing key"}

        existing = self._deferred.pop(key, None)
        if existing and not existing.done():
            existing.cancel()

        async def _fire():
            try:
                await asyncio.sleep(delay)
                await self.do_notify(text)
                log.info("deferred DM fired for key=%s", key)
            except asyncio.CancelledError:
                log.info("deferred DM cancelled for key=%s", key)
                raise
            except Exception as e:
                log.warning("deferred DM failed for key=%s: %s", key, e)
            finally:
                self._deferred.pop(key, None)

        task = asyncio.create_task(_fire())
        self._deferred[key] = task
        log.info("deferred DM scheduled key=%s delay=%ss", key, delay)
        return {"ok": True, "scheduled": True}

    async def do_cancel_deferred(self, key: str) -> dict:
        """Cancel a pending deferred DM. If key is empty/None, cancel all."""
        if not key:
            cancelled = 0
            for k, task in list(self._deferred.items()):
                if not task.done():
                    task.cancel()
                    cancelled += 1
                self._deferred.pop(k, None)
            return {"ok": True, "cancelled": cancelled}

        task = self._deferred.pop(key, None)
        if task and not task.done():
            task.cancel()
            return {"ok": True, "cancelled": 1}
        return {"ok": True, "cancelled": 0}

    async def do_ask(self, text: str, options: list, timeout: float) -> dict:
        await self._wait_ready(timeout=30)
        channel = await asyncio.to_thread(open_dm, self.token, self.user_id)
        components = self._build_components(options) if options else None
        msg = await asyncio.to_thread(send_message, self.token, channel, text, components)
        message_id = msg["id"]

        request_id = secrets.token_hex(8)
        future = asyncio.get_running_loop().create_future()
        # Pass channel_id and original text so we can edit the message later
        # (on timeout, or to show what was answered).
        self.pending.add(request_id, message_id, future, options,
                         channel_id=channel, original_text=text)
        log.info("ask: posted message %s, awaiting reply (timeout=%ss)", message_id, timeout)

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return {"ok": True, "answer": result["answer"]}
        except asyncio.TimeoutError:
            # Pull the entry without resolving the future so we still have
            # channel_id/message_id for cleanup.
            entry = self.pending.cancel(request_id)
            if entry and entry.get("channel_id") and entry.get("message_id"):
                original = entry.get("original_text") or "(question)"
                new_text = (
                    f"❓ {original}\n"
                    f"⏱️ **Timed out after {int(timeout)}s — answered elsewhere or no longer needed.**"
                )
                asyncio.create_task(asyncio.to_thread(
                    edit_message, self.token,
                    entry["channel_id"], entry["message_id"],
                    new_text, True,
                ))
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
                elif cmd == "defer_notify":
                    resp = await daemon.do_defer_notify(
                        req.get("key", ""),
                        req.get("text", ""),
                        float(req.get("delay") or 30),
                    )
                elif cmd == "cancel_deferred":
                    resp = await daemon.do_cancel_deferred(req.get("key", ""))
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
    """Remove our info/pid files — but ONLY if they reference our PID.

    Critical safety: if another daemon process owns these files, we must NOT
    delete them. Without this guard, a second-daemon-attempt would clobber the
    first daemon's state when it exits via the single-instance guard.
    """
    my_pid = os.getpid()
    for p in (INFO_FILE, PID_FILE):
        try:
            if not p.exists():
                continue
            owner_pid = None
            try:
                raw = p.read_text(encoding="utf-8").strip()
                if p == INFO_FILE:
                    owner_pid = int(json.loads(raw).get("pid", 0))
                else:
                    owner_pid = int(raw)
            except Exception:
                # Unparseable; treat as orphan and remove (only safe if we're
                # the one starting up — but here we're in cleanup. Skip.)
                continue
            if owner_pid == my_pid:
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

    # Single-instance guard: if another daemon is already running AND its
    # TCP port is accepting connections, exit immediately. Without this,
    # repeated client invocations on Windows (where os.kill liveness checks
    # are unreliable) accumulate orphan daemons.
    if _another_daemon_alive():
        log.info("another daemon is already running and healthy; exiting")
        sys.exit(0)

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


def _another_daemon_alive() -> bool:
    """Check if INFO_FILE points at a healthy running daemon (PID alive AND
    port answering)."""
    try:
        if not INFO_FILE.exists():
            return False
        info = json.loads(INFO_FILE.read_text(encoding="utf-8"))
    except Exception:
        return False
    pid = info.get("pid", 0)
    port = info.get("port", 0)
    if not pid or not port:
        return False
    # Liveness — use the same cross-platform PID check as the client.
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            STILL_ACTIVE = 259
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                running = (exit_code.value == STILL_ACTIVE)
            else:
                running = True
            kernel32.CloseHandle(handle)
            if not running:
                return False
        except Exception:
            return False
    else:
        try:
            os.kill(int(pid), 0)
        except OSError:
            return False
    # Liveness OK — confirm TCP port answers.
    import socket as _socket
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(("127.0.0.1", int(port)))
        s.close()
        return True
    except (OSError, _socket.timeout):
        return False


if __name__ == "__main__":
    import atexit
    # Always try to clean up our pid/info files on exit, even on hard kills
    # where the asyncio finally clause didn't run.
    atexit.register(cleanup)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
