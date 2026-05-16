#!/usr/bin/env python3
"""
Single-purpose credential test for cc-discord setup.

Usage:
    python test_discord.py <bot_token> <user_id>

Sends a real test DM via Discord's REST API and classifies the result.
Last line of stdout is one of:
    OK
    ERR:auth         (bot token invalid)
    ERR:user         (user_id doesn't exist, or wrong format)
    ERR:missing-dm   (the bot can't DM this user — they don't share a server,
                      or the user has DMs from server members disabled)
    ERR:unknown      (anything else)

Cross-platform: uses urllib (stdlib only).
"""
import json
import os
import sys
import ssl
import urllib.request
import urllib.error

API_BASE = "https://discord.com/api/v10"
UA = "ClaudeCodeDiscordNotifier (https://github.com/treeman0/claude-code-discord-notifications, 1.0)"


def _build_ssl_context():
    cafile = os.environ.get("SSL_CERT_FILE")
    if cafile and os.path.exists(cafile):
        return ssl.create_default_context(cafile=cafile)
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        pass
    return ssl.create_default_context()

SSL_CONTEXT = _build_ssl_context()


def post(path: str, token: str, body: dict):
    """POST to Discord. Returns (status_code, parsed_body_dict_or_text)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{API_BASE}{path}", data=data, method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw) if raw else {}
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
            return e.code, json.loads(raw) if raw else {}
        except Exception:
            return e.code, ""
    except urllib.error.URLError as e:
        return -1, str(e)


def classify(status: int, body) -> str:
    """Map a Discord response to one of our classifier strings."""
    if status == 401:
        return "ERR:auth"
    code = body.get("code") if isinstance(body, dict) else None
    if code == 50007:
        return "ERR:missing-dm"
    if code in (10013, 50035):
        return "ERR:user"
    if status in (200, 201):
        return "OK"
    return "ERR:unknown"


def main(argv):
    if len(argv) != 3:
        print("usage: test_discord.py <bot_token> <user_id>", file=sys.stderr)
        return 2
    token, user_id = argv[1], argv[2]

    # Step 1: open the DM channel.
    status, body = post("/users/@me/channels", token, {"recipient_id": user_id})
    print(f"step 1 (open DM): http={status}")
    print(json.dumps(body, indent=2) if isinstance(body, dict) else str(body))
    verdict = classify(status, body)
    if verdict != "OK":
        print(verdict)
        return 0

    channel_id = body.get("id") if isinstance(body, dict) else None
    if not channel_id:
        print("ERR:unknown")
        return 0

    # Step 2: post a message.
    status, body = post(
        f"/channels/{channel_id}/messages", token,
        {"content": "**cc-discord** setup test ✅\n"
                    "If you see this, the credentials are working."},
    )
    print(f"step 2 (send msg): http={status}")
    print(json.dumps(body, indent=2) if isinstance(body, dict) else str(body))
    # 50007 on send-message means the user blocks DMs from non-friends.
    print(classify(status, body))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
