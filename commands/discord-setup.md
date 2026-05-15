---
description: Set up Discord credentials for cc-discord notifications
allowed-tools: Bash($CLAUDE_PLUGIN_ROOT/scripts/test-discord.sh:*), Bash(python3:*), Bash(pip3:*), Bash(pip:*), Bash(chmod:*), Bash(rm:*), Read, Write
---

# Discord setup for Claude Code

You are walking the user through setting up Discord notifications for Claude Code. Be conversational, friendly, and brief — don't recite this whole doc at them.

## Goal

By the end of this conversation:

1. `~/.claude/.discord.env` exists with `DISCORD_BOT_TOKEN` and `DISCORD_USER_ID`.
2. The Python `websockets` package is installed.
3. A test DM has arrived in the user's Discord.

## Tools you'll use

- `$CLAUDE_PLUGIN_ROOT/scripts/test-discord.sh <token> <user_id>` — sends a real test DM. Last line of output is `OK`, `ERR:auth`, `ERR:user`, `ERR:missing-dm`, or `ERR:unknown`.
- `python3 -c "import websockets"` — check the dependency is installed.
- `python3 -m pip install --user websockets` — install it.

## Step 1 — Existing setup?

Read `~/.claude/.discord.env`. If it exists, show what's there (mask the token: first 8 chars + `***` + last 4). Ask whether to reconfigure or just re-test. If just re-test, jump to step 6.

## Step 2 — Install websockets

Check: `python3 -c "import websockets; print(websockets.__version__)"`

If it fails, install: `python3 -m pip install --user websockets`. If that errors with "externally-managed-environment", try `python3 -m pip install --user --break-system-packages websockets`. If that fails too, suggest `pipx install websockets` or installing via the user's package manager.

Don't proceed until the import works.

## Step 3 — Create the Discord bot

Walk through it patiently — most people haven't done this:

1. Open https://discord.com/developers/applications
2. Click **New Application** (top right). Name it anything ("Claude Code Notifier"). Accept terms.
3. Left sidebar → **Bot**.
4. Click **Reset Token** → **Yes, do it!** → **Copy** the token.
   ⚠️ This token is shown ONCE. If they navigate away without copying, they'll need to reset it again.
5. Scroll down to **Privileged Gateway Intents**. They do NOT need any of these — leave them off.

Get the token from them. Don't print it back in chat.

## Step 4 — Get the user's Discord user ID

Two ways:

- **Easy**: Discord → click their profile picture (top-left) → **Profile** → `...` next to username → **Copy User ID**.
- If they don't see "Copy User ID", they need to enable Developer Mode: **User Settings** (gear) → **Advanced** → toggle **Developer Mode** on.

Get the user_id. It's a long number, ~17-19 digits.

## Step 5 — Invite the bot so it can DM you

Discord bots can only DM users who share a server with them. This is the step most people get stuck on:

1. Tell them to pick a server where they can add the bot, or create a private server just for themselves (Discord sidebar → "+" → "Create My Own" → "For me and my friends").

2. Back at https://discord.com/developers/applications → their bot:
   - Left sidebar → **OAuth2** → **OAuth2 URL Generator** (or **Installation** → "Install Link" in newer UI)
   - **SCOPES**: check `bot`
   - **BOT PERMISSIONS**: check `Send Messages`
   - Copy the generated URL at the bottom
   - Open the URL, pick their server, click **Authorize**

3. After authorizing, the bot appears in the server's member list. It's offline (that's fine — we'll bring it online when the daemon starts).

## Step 6 — Save credentials

Write `~/.claude/.discord.env`:

```
# cc-discord credentials. Do not commit.
DISCORD_BOT_TOKEN="<token>"
DISCORD_USER_ID="<user_id>"
```

Then: `chmod 600 ~/.claude/.discord.env` and `rm -f ~/.claude/cc-discord.NEEDS_SETUP`.

## Step 7 — Test

Run: `$CLAUDE_PLUGIN_ROOT/scripts/test-discord.sh <token> <user_id>`

Parse the last line:

- `OK` → success. The user has a DM. Move to step 8.
- `ERR:auth` → token is wrong. Show the response. Ask them to re-copy from the developer portal.
- `ERR:user` → user_id is wrong format or doesn't exist. Ask them to copy again — THEIR id, not the bot's.
- `ERR:missing-dm` → the bot and user don't share a server, or the user has DMs blocked. Walk them through step 5 again. Also: in their server's **Privacy Settings**, ensure "Direct Messages from server members" is on.
- `ERR:unknown` → show the response and ask if they want to retry or save anyway.

## Step 8 — Done

Tell the user:

- Setup complete.
- They'll now get a Discord DM when Claude finishes a turn, asks a question, requests permission, or hits an API error.
- They can ask Claude to use Discord round-trip: `/ask-discord <question>` with optional `-- option1 | option2` for tappable buttons.
- The daemon auto-starts on first hook fire and stays running. To check: `python3 $CLAUDE_PLUGIN_ROOT/bin/discord-client.py status`. To stop: `python3 $CLAUDE_PLUGIN_ROOT/bin/discord-client.py stop`.
- To silence temporarily: `/plugin disable claude-code-discord-notifications@treeman0`.

## Tone

Short. One step at a time. Help when stuck. Don't lecture.
