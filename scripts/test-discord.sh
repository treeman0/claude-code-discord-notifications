#!/usr/bin/env bash
# Single-purpose credential test for cc-discord setup.
#
# Usage: test-discord.sh <bot_token> <user_id>
#
# Sends a real DM via Discord's REST API and classifies the result.
# Last line of output is one of:
#   OK
#   ERR:auth         (bot token invalid)
#   ERR:user         (user_id doesn't exist, or wrong format)
#   ERR:missing-dm   (the bot can't DM this user — they don't share a server,
#                     or the user has DMs from server members disabled)
#   ERR:unknown      (anything else)

set -euo pipefail

TOKEN="${1:-}"
USER_ID="${2:-}"

if [[ -z "$TOKEN" || -z "$USER_ID" ]]; then
  echo "usage: $0 <bot_token> <user_id>" >&2
  exit 2
fi

UA="ClaudeCodeDiscordNotifier (https://github.com/treeman0/claude-code-discord-notifications, 1.0)"

# Step 1: open the DM channel.
RESP="$(curl -sS --max-time 10 -X POST \
  "https://discord.com/api/v10/users/@me/channels" \
  -H "Authorization: Bot $TOKEN" \
  -H "Content-Type: application/json" \
  -H "User-Agent: $UA" \
  -d "{\"recipient_id\":\"$USER_ID\"}" \
  -w '\n__HTTP__:%{http_code}' 2>&1 || true)"
echo "$RESP"

HTTP_CODE="$(echo "$RESP" | grep '__HTTP__:' | tail -1 | sed 's/.*__HTTP__://')"
BODY="$(echo "$RESP" | sed '/__HTTP__:/d')"

# 401: bad token. 50007: cannot send messages to this user. 10013: unknown user.
if [[ "$HTTP_CODE" == "401" ]]; then
  echo "ERR:auth"; exit 0
fi
if echo "$BODY" | grep -q '"code": *50007'; then
  echo "ERR:missing-dm"; exit 0
fi
if echo "$BODY" | grep -qE '"code": *(10013|50035)'; then
  echo "ERR:user"; exit 0
fi
if [[ "$HTTP_CODE" != "200" && "$HTTP_CODE" != "201" ]]; then
  echo "ERR:unknown"; exit 0
fi

# Step 2: parse the channel id and post a test message.
CHANNEL_ID="$(echo "$BODY" | jq -r '.id // empty' 2>/dev/null)"
if [[ -z "$CHANNEL_ID" ]]; then
  echo "ERR:unknown"; exit 0
fi

RESP2="$(curl -sS --max-time 10 -X POST \
  "https://discord.com/api/v10/channels/$CHANNEL_ID/messages" \
  -H "Authorization: Bot $TOKEN" \
  -H "Content-Type: application/json" \
  -H "User-Agent: $UA" \
  -d '{"content":"**cc-discord** setup test ✅\nIf you see this, the credentials are working."}' \
  -w '\n__HTTP__:%{http_code}' 2>&1 || true)"
echo "$RESP2"

HTTP_CODE2="$(echo "$RESP2" | grep '__HTTP__:' | tail -1 | sed 's/.*__HTTP__://')"
BODY2="$(echo "$RESP2" | sed '/__HTTP__:/d')"

if echo "$BODY2" | grep -q '"code": *50007'; then
  echo "ERR:missing-dm"; exit 0
fi
if [[ "$HTTP_CODE2" == "200" || "$HTTP_CODE2" == "201" ]]; then
  echo "OK"; exit 0
fi
echo "ERR:unknown"
