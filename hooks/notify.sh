#!/usr/bin/env bash
# cc-discord notification hook.
#
# Fires on Stop, Notification (permission_prompt|idle_prompt), and StopFailure.
# Reads Claude Code's hook JSON from stdin, builds a short message, sends it
# to the daemon over the Unix socket. The daemon DMs you on Discord.
#
# Credentials live in ~/.claude/.discord.env — NOT in the plugin directory,
# since plugin updates wipe the cache but credentials shouldn't disappear.

set -euo pipefail

ENV_FILE="${CC_DISCORD_ENV_FILE:-$HOME/.claude/.discord.env}"

# First-run hint: if no env file, drop a note where the user will see it.
if [[ ! -f "$ENV_FILE" ]]; then
  HINT_FILE="$HOME/.claude/cc-discord.NEEDS_SETUP"
  if [[ ! -f "$HINT_FILE" ]]; then
    mkdir -p "$HOME/.claude"
    cat > "$HINT_FILE" <<'EOF'
cc-discord is installed but has no credentials yet.
Run the setup command in Claude Code:

    /discord-setup

Or remove this file to silence the reminder.
EOF
  fi
  exit 0
fi

# Once configured, nuke the hint.
rm -f "$HOME/.claude/cc-discord.NEEDS_SETUP" 2>/dev/null || true

LOG="$HOME/.claude/cc-discord.log"
log() {
  [[ "${CC_DISCORD_DEBUG:-0}" == "1" ]] && \
    printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$LOG" || true
}

PAYLOAD="$(cat || true)"
[[ -z "$PAYLOAD" ]] && exit 0
log "payload: $PAYLOAD"

get() {
  printf '%s' "$PAYLOAD" | jq -r "$1 // empty" 2>/dev/null || true
}

EVENT="$(get '.hook_event_name')"
CWD="$(get '.cwd')"
MSG="$(get '.message')"
NOTIF_TYPE="$(get '.notification_type')"
STOPFAIL_REASON="$(get '.reason')"

TITLE=""; BODY=""
case "$EVENT" in
  Stop)
    TITLE="✅ Claude Code finished"
    BODY="The agent finished its turn and is idle."
    ;;
  Notification)
    case "$NOTIF_TYPE" in
      permission_prompt)
        TITLE="🔐 Claude Code wants permission"
        BODY="${MSG:-Claude is asking to use a tool.}"
        ;;
      idle_prompt)
        TITLE="❓ Claude Code is waiting on you"
        BODY="${MSG:-Claude is asking a question.}"
        ;;
      *)
        TITLE="🔔 Claude Code notification"
        BODY="${MSG:-(no message)}"
        ;;
    esac
    ;;
  StopFailure)
    case "$STOPFAIL_REASON" in
      rate_limit)            TITLE="⏳ Claude Code: rate limited" ;;
      authentication_failed) TITLE="🔑 Claude Code: auth failed" ;;
      oauth_org_not_allowed) TITLE="🔑 Claude Code: org not allowed" ;;
      billing_error)         TITLE="💳 Claude Code: billing error" ;;
      max_output_tokens)     TITLE="📏 Claude Code: hit output token limit" ;;
      invalid_request)       TITLE="⚠️ Claude Code: invalid request" ;;
      server_error)          TITLE="🛑 Claude Code: server error" ;;
      *)                     TITLE="🛑 Claude Code stopped with an error" ;;
    esac
    BODY="Reason: ${STOPFAIL_REASON:-unknown}. The turn ended early — restart or fix the underlying issue."
    ;;
  *)
    TITLE="🔔 Claude Code: ${EVENT:-event}"
    BODY="${MSG:-(no message)}"
    ;;
esac

# Discord allows 2000 chars per message; 800 is plenty for a notification.
if [[ ${#BODY} -gt 800 ]]; then
  BODY="${BODY:0:797}..."
fi

# Discord markdown: **bold**, _italic_.
TEXT="**${TITLE}**"$'\n'"${BODY}"
if [[ "${CC_DISCORD_INCLUDE_DIR:-1}" != "0" && -n "$CWD" ]]; then
  TEXT+=$'\n''_'"${CWD}"'_'
fi

# Find client + daemon (siblings in this plugin's bin/).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
CLIENT="$PLUGIN_ROOT/bin/discord-client.py"
DAEMON="$PLUGIN_ROOT/bin/discord-daemon.py"

PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
  log "python3 not on PATH; skipping discord notification"
  exit 0
fi

# Fire-and-forget. Client lazy-starts the daemon if needed.
CC_DISCORD_DAEMON="$DAEMON" "$PY" "$CLIENT" notify "$TEXT" 2>&1 | head -5 >> "$LOG" || true

exit 0
