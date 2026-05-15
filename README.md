# claude-code-discord-notifications

A Claude Code plugin that DMs you on **Discord** when Claude:

- ✅ **finishes** a turn
- ❓ **asks you a question** or goes idle waiting
- 🔐 **wants permission** to run a tool
- 🛑 **hits an API error** that needs your attention (rate limit, auth, billing, server, max output tokens)

And, optionally — the part you actually came for — **Claude can ask you a question on Discord and wait for your reply, with tappable buttons**. You tap an option on your phone (or type a free-form answer), Claude reads it back, conversation continues.

## Install

Inside Claude Code:

```
/plugin marketplace add treeman0/claude-code-discord-notifications
/plugin install claude-code-discord-notifications@treeman0
/discord-setup
```

The wizard walks you through creating a Discord bot, getting your user ID, inviting the bot to a server you share with it, and verifying everything end-to-end. ~5 minutes.

## How round-trip works

Once set up, you have a new command: `/ask-discord`.

```
/ask-discord Should I deploy? -- yes | no | wait
```

Claude sends a DM to your phone with three tappable buttons. The command **blocks** until you tap one (or reply with text), then the answer appears in Claude's context and the conversation continues. Free-form works too:

```
/ask-discord What should the commit message be?
```

You type the answer back on Discord, Claude reads it. Default timeout is 10 minutes; pass `--timeout 1800` for 30.

You can also instruct Claude to use it itself — e.g. in your project's `CLAUDE.md`:

> When you need a decision from me and I might be away from the keyboard, use `/ask-discord` with buttons instead of stopping and waiting.

## What gets installed

Three hooks register automatically when you install the plugin:

| Event | What triggers it |
| --- | --- |
| `Stop` | Claude finishes a turn |
| `Notification` (`permission_prompt`, `idle_prompt`) | Claude needs your input |
| `StopFailure` | API error ended the turn |

Plus three slash commands: `/discord-setup`, `/ask-discord`, `/discord-status`.

`PostToolUseFailure` is off by default — it fires on every failed tool call, including ones Claude recovers from on its own. To enable, add a hook for it in `~/.claude/settings.json`.

## Architecture

A small Python daemon (`discord-daemon.py`) keeps a persistent WebSocket connection to Discord's gateway. The hook script and slash commands talk to it over a Unix socket at `~/.claude/discord-daemon.sock`. The daemon owns:

- Sending DMs (one-way notifications)
- Posting messages with action-row buttons
- Listening for button clicks (`INTERACTION_CREATE`) and DM replies (`MESSAGE_CREATE` in DM channels)
- Matching replies back to open questions and signaling waiting CLI clients

**No public IP, no tunnel, no webhook.** Discord's gateway is a single outbound WebSocket — replies arrive in real time without anything special on your side. This is why Discord works for round-trip where WhatsApp doesn't.

**No privileged intents needed.** Discord delivers DM message content to bots without the `MESSAGE_CONTENT` intent, and button clicks arrive as `INTERACTION_CREATE` which doesn't need any intent at all. So the bot's permissions are minimal: it can DM you, you can DM it back, and it cannot read anything else.

## Daemon lifecycle

The daemon is **lazy-started** the first time the hook or `/ask-discord` runs. It then keeps running until you reboot or explicitly stop it. Check status:

```
/discord-status
```

Or directly:

```bash
python3 ~/.claude/plugins/cache/claude-code-discord-notifications/<version>/bin/discord-client.py status
python3 ~/.claude/plugins/cache/claude-code-discord-notifications/<version>/bin/discord-client.py stop
```

The daemon writes a PID file at `~/.claude/discord-daemon.pid` and logs to `~/.claude/discord-daemon.log`.

## Requirements

- **Python 3.8+** (everywhere except Windows-without-WSL, which won't work — the Unix socket isn't supported on native Windows)
- **`websockets`** package — `/discord-setup` installs it for you via `pip install --user`
- **`curl` and `jq`** on PATH (macOS: `curl` ships, `brew install jq`. Debian/Ubuntu: `sudo apt install jq`)
- A **Discord account** and a **server you can invite the bot to** (a private server-of-one works fine)

## Customize

Variables you can set in `~/.claude/.discord.env`:

- `CC_DISCORD_INCLUDE_DIR=0` — drop the working-directory line from messages
- `CC_DISCORD_DEBUG=1` — verbose logging to `~/.claude/cc-discord.log`

The `Stop` hook fires every turn, which can be chatty. To get pings only for questions / permissions / errors, remove the `Stop` block from `~/.claude/settings.json` or disable just it via `/hooks`.

## Update

```
/plugin marketplace update treeman0
/plugin update claude-code-discord-notifications@treeman0
```

If the daemon's running, stop and restart it after updating: `python3 .../bin/discord-client.py stop`.

## Uninstall

```
/plugin uninstall claude-code-discord-notifications@treeman0
```

Removes the plugin and unregisters the hooks. Credentials at `~/.claude/.discord.env` stay — delete that yourself if you're done. Stop the daemon manually too:

```
python3 ~/.claude/plugins/cache/claude-code-discord-notifications/*/bin/discord-client.py stop
```

## Files

```
.claude-plugin/
  marketplace.json     marketplace catalog
  plugin.json          plugin manifest
hooks/
  hooks.json           hook registrations (auto-applied)
  notify.sh            fires on Stop/Notification/StopFailure
commands/
  discord-setup.md     /discord-setup wizard
  ask-discord.md       /ask-discord round-trip question
  discord-status.md    /discord-status daemon health
bin/
  discord-daemon.py    long-running gateway WebSocket client
  discord-client.py    CLI: talks to the daemon over Unix socket
scripts/
  test-discord.sh      credential test helper (used by /discord-setup)
```

## Troubleshooting

**The daemon won't start.** Check `~/.claude/discord-daemon.log`. Most likely: `websockets` isn't installed (`python3 -m pip install --user websockets`), or the env file is missing/malformed.

**I tap a button but Claude doesn't continue.** The daemon should ACK the button click within 3 seconds. If you see the button greyed out / message edited to show the answer, the daemon got it. If `/ask-discord` is still hanging, check that the daemon's pending count went down: `/discord-status`.

**No DM arrives.** Run `/discord-setup` to re-test credentials, or directly: `bash $CLAUDE_PLUGIN_ROOT/scripts/test-discord.sh <token> <user_id>`. The most common cause is the bot not sharing a server with you (step 5 of setup).

**Daemon hangs after a long period.** Discord's gateway sends `RECONNECT` (op 7) periodically; the daemon handles this. If something pathological happens, just stop and restart it.
