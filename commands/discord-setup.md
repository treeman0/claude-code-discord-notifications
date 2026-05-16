---
description: Set up Discord credentials for cc-discord notifications
allowed-tools: Bash($CLAUDE_PLUGIN_ROOT/scripts/test_discord.py:*), Bash(python3:*), Bash(python:*), Bash(py:*), Bash(pip:*), Bash(pip3:*), Bash(chmod:*), Bash(rm:*), Bash(which:*), Bash(where:*), Read, Write
---

# Discord setup for Claude Code

You are walking the user through setting up Discord notifications for Claude Code. Be conversational, friendly, and brief — don't recite this whole doc at them.

## Goal

By the end of this conversation:

1. `~/.claude/.discord.env` exists with `DISCORD_BOT_TOKEN` and `DISCORD_USER_ID`.
2. The Python `websockets` package is installed and importable as `python3`.
3. A test DM has arrived in the user's Discord.

## Tools you'll use

- `python3 "$CLAUDE_PLUGIN_ROOT/scripts/test_discord.py" <token> <user_id>` — sends a real test DM. Last line of stdout is `OK`, `ERR:auth`, `ERR:user`, `ERR:missing-dm`, or `ERR:unknown`.
- `python3 -c "import websockets"` — check the dependency.
- `python3 -m pip install --user websockets` — install it.

## Step 1 — Existing setup?

Read `~/.claude/.discord.env`. If it exists, show what's there (mask the token: first 8 chars + `***` + last 4). Ask whether to reconfigure or just re-test. If just re-test, jump to step 6.

## Step 2 — Verify Python and install dependencies

This is the most error-prone step on Windows. Be careful here:

First, check which python3 is on PATH:
- Linux/macOS: `which python3`
- Windows (git-bash/MSYS): `which python3` — note that on Windows this may resolve to MSYS's Python, which often lacks pip. Also run `where python python3 py` (use `cmd //c "where ..."` from git-bash) to see all Pythons available.

Then check if websockets imports: `python3 -c "import websockets; print(websockets.__version__)"`

Install BOTH `websockets` AND `certifi`:
- `python3 -m pip install --user websockets certifi`
- If that fails with `externally-managed-environment`: `python3 -m pip install --user --break-system-packages websockets certifi`
- If `pip` is missing entirely (`No module named pip`) — that's the MSYS-Python-without-pip problem; see below.

**Why certifi too?** Python on Windows often can't verify Discord's TLS cert against the OS cert store, which breaks the gateway WebSocket with `CERTIFICATE_VERIFY_FAILED`. `certifi` ships the Mozilla CA bundle that the daemon falls back to.

**The MSYS-Python problem (Windows):** if the user's `python3` is `/c/msys64/mingw64/bin/python3` and has no pip, the cleanest fix is to use the Windows Python installation (`py` launcher) and route `python3` to it:

1. Check Windows Python: `py -c "import sys; print(sys.version)"` — should succeed if they have a normal Python install.
2. Install websockets there: `py -m pip install --user websockets`
3. Create a `python3` shim. Tell the user to run this from cmd/PowerShell (NOT git-bash):
   ```
   echo @py -3 %%* > "%USERPROFILE%\AppData\Local\Microsoft\WindowsApps\python3.bat"
   ```
   (This puts a python3.bat shim in a dir already on Windows PATH.)
4. Verify in a NEW git-bash window: `which python3` should now show `/c/Users/<name>/AppData/Local/Microsoft/WindowsApps/python3.bat`, and `python3 -c "import websockets; print(websockets.__version__)"` should succeed.
5. The user must restart Claude Code after this so it picks up the new PATH.

Other Windows fallbacks if the user doesn't want the shim approach:
- Install pip into MSYS Python: open an MSYS terminal and run `pacman -S mingw-w64-x86_64-python-pip`, then `pip install websockets`.
- Install pipx and use `pipx install websockets` — works but pipx is meant for apps, not libs, so this is hacky.

Don't proceed past this step until `python3 -c "import websockets"` exits 0.

## Step 3 — Create the Discord bot

Walk through it patiently:

1. Open https://discord.com/developers/applications
2. Click **New Application** (top right). Name it ("Claude Code Notifier"). Accept terms.
3. Left sidebar → **Bot**.
4. Click **Reset Token** → **Yes, do it!** → **Copy** the token.
   ⚠️ Token is shown ONCE. If they navigate away without copying, they have to reset it again.
5. Scroll down to **Privileged Gateway Intents**. They do NOT need any of these — leave them off.

Get the token from them. Don't echo it back in chat.

## Step 4 — Get the user's Discord user ID

Two ways:

- **Easy**: Discord → click their profile picture (top-left) → **Profile** → `...` next to username → **Copy User ID**.
- If they don't see "Copy User ID", enable Developer Mode first: **User Settings** (gear) → **Advanced** → toggle **Developer Mode** on.

User_id is a long number, ~17-19 digits.

## Step 5 — Invite the bot so it can DM you

Discord bots can only DM users who share a server with them:

1. Pick a server they can add the bot to, or create a private one (Discord sidebar → "+" → "Create My Own" → "For me and my friends").

2. Back at https://discord.com/developers/applications → their bot → left sidebar → **OAuth2** → **OAuth2 URL Generator** (newer UI: **Installation**):
   - **SCOPES**: check **BOTH** `bot` AND `applications.commands`
     - `bot` lets the bot send messages
     - `applications.commands` lets the bot **respond to button taps** (without it, every button shows "This interaction failed" on your phone)
   - **BOT PERMISSIONS**: check `Send Messages`
   - Copy the generated URL
   - Open it, pick the server, click **Authorize**

3. After authorizing, the bot appears in the server's member list. It's offline (fine — comes online when the daemon starts).

> If you already invited the bot with only `bot` and your buttons aren't working: redo this step with `applications.commands` also checked, then open the new URL and click Authorize again. Discord will update the existing install — you don't need to remove and re-add.

## Step 6 — Save credentials

Write `~/.claude/.discord.env`:

```
# cc-discord credentials. Do not commit.
DISCORD_BOT_TOKEN="<token>"
DISCORD_USER_ID="<user_id>"
```

Then on macOS/Linux: `chmod 600 ~/.claude/.discord.env`. On Windows: skip chmod (Windows ignores it).

Then: `rm -f ~/.claude/cc-discord.NEEDS_SETUP` to clear the install reminder.

## Step 7 — Test

Run: `python3 "$CLAUDE_PLUGIN_ROOT/scripts/test_discord.py" <token> <user_id>`

Parse the last line:

- `OK` → success. User has a DM. Move to step 8.
- `ERR:auth` → token is wrong. Ask to re-copy from the developer portal.
- `ERR:user` → user_id is wrong. Ask to copy again — THEIR id, not the bot's.
- `ERR:missing-dm` → bot and user don't share a server, or user blocks server DMs. Walk through step 5 again, and confirm **Privacy Settings** → **Direct Messages from server members** is on.
- `ERR:unknown` → show the full response and ask if they want to retry or save anyway.

## Step 8 — Done

Tell the user:

- Setup is complete.
- They'll now get a Discord DM when Claude finishes a turn, asks a question, requests permission, or hits an API error.
- They can use Discord round-trip: `/ask-discord <question>` with optional `-- option1 | option2` for tappable buttons.
- The daemon auto-starts on first hook fire and stays running. Check: `python3 "$CLAUDE_PLUGIN_ROOT/bin/discord-client.py" status`. Stop: `python3 "$CLAUDE_PLUGIN_ROOT/bin/discord-client.py" stop`.
- To silence temporarily: `/plugin disable claude-code-discord-notifications@treeman0`.

## Tone

Short. One step at a time. Help if stuck. Don't lecture.
