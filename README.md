# claude-code-discord-notifications

A Claude Code plugin that makes Discord a **remote co-pilot** for Claude Code. Claude DMs you on Discord when it:

- ✅ **finishes** a turn (with a summary of tools used)
- ❓ **asks a question** or goes idle waiting
- 🔐 **wants to run a tool** that isn't on the auto-allow list — you tap `✅ Approve` or `❌ Deny` on your phone, Claude proceeds or stops accordingly
- 🛑 **hits an API error** (rate limit, auth, billing, server, max output tokens)

Plus: **round-trip questions** — Claude can ask you a question on Discord with tappable buttons (or free-form text), and wait for your answer. Run `/ask-discord` yourself, or tell Claude (in `CLAUDE.md`) to use it when you might be away from the keyboard.

Cross-platform: macOS, Linux, and Windows (Git Bash / MSYS / native).

## How tool approval works

The plugin registers a `PreToolUse` hook. Before Claude can run anything that modifies state, the hook checks an auto-allow list:

- **Auto-allowed (no DM):** `Read`, `Glob`, `Grep`, `TodoWrite`, `WebFetch`, `WebSearch`, `NotebookRead`, plus safe Bash commands (`ls`, `cat`, `git status/log/diff/branch`, `npm test`, `pytest`, `cargo test`, `*--version`, etc.). The Bash list rejects anything with `>`, `<`, `|`, `&`, `;`, `$(...)`, backticks, or `&&`/`||` — even if the leading command is safe.
- **Everything else:** DMed to you with two buttons. Default 60-second window for you to answer. Approve → Claude proceeds. Deny → Claude stops with the reason "Denied on Discord."
- **If Discord is unreachable** (daemon down, phone offline, no answer within the timeout): falls back to Claude Code's normal in-terminal prompt. No silent allows.

### Claude's clarifying questions (`AskUserQuestion`)

When Claude uses the `AskUserQuestion` tool (it does this often in plan mode), the plugin recognizes it and sends each of Claude's questions to Discord with **the actual options Claude offered as tappable buttons** — not just approve/deny.

- One DM per question. With multiple questions, an intro DM lists them all first so you know what's coming.
- Buttons match Claude's actual labels ("Immediate failure", "With retry", "Custom handler", etc.). Tap one, that becomes the answer.
- For multi-select questions: tap one option, or reply with a comma-separated list of labels (`"Python, Rust"`).
- Free-text replies are matched case-insensitively against labels (so typing `yes` matches button `Yes`).
- Default 180-second timeout per question.
- If Discord is unreachable or your reply can't be matched to a label, falls back to the normal in-terminal prompt for that question.

Configure via env vars in `~/.claude/.discord.env`:

```
CC_DISCORD_PERMISSION=off              # disable; behave like the base notification plugin
CC_DISCORD_PERMISSION_TIMEOUT=120      # seconds to wait for Discord reply
CC_DISCORD_PERMISSION_SKIP=Read,Glob   # override the auto-allow tool list
CC_DISCORD_PERMISSION_BASH_SAFE=...    # override the safe-Bash regex
CC_DISCORD_TICKER=off                  # disable the per-turn tool-use summary in Stop DMs
```

## Install

Inside Claude Code:

```
/plugin marketplace add treeman0/claude-code-marketplace
/plugin install claude-code-discord-notifications@treeman0
/discord-setup
```

`/discord-setup` walks you through creating a Discord bot, getting your user ID, inviting the bot to a server you share, and verifying everything works. ~5 minutes.

## How round-trip works

Once set up, you have a new command: `/ask-discord`.

```
/ask-discord Should I deploy? -- yes | no | wait
```

Claude sends a DM to your phone with three tappable buttons. The command **blocks** until you tap one (or reply with text), then the answer appears in Claude's context. Free-form works too:

```
/ask-discord What should the commit message be?
```

You type the answer back on Discord, Claude reads it. Default timeout 10 min; pass `--timeout 1800` for 30.

You can also instruct Claude to use it itself — e.g. in your project's `CLAUDE.md`:

> When you need a decision from me and I might be away from the keyboard, use `/ask-discord` with buttons instead of stopping and waiting.

## Architecture

A small Python daemon (`discord-daemon.py`) keeps a persistent WebSocket connection to Discord's gateway. The hook script and slash commands talk to it over a **TCP loopback socket** (127.0.0.1:`<random_port>`). The daemon writes its port + auth token to `~/.claude/discord-daemon.info` on startup; clients read that file to find the daemon.

Why TCP loopback and not Unix sockets? CPython on Windows doesn't expose `socket.AF_UNIX` (it's an open issue from 2018). TCP loopback works identically on every platform.

What runs:

- **Sending DMs** (one-way notifications)
- **Posting messages with action-row buttons**
- **Listening for button clicks** (`INTERACTION_CREATE`) and **DM replies** (`MESSAGE_CREATE` in DM channels)
- **Matching replies back to open questions** and signaling waiting CLI clients

**No public IP, no tunnel, no webhook.** Discord's gateway is a single outbound WebSocket — replies arrive in real time without anything special on your network.

**No privileged intents needed.** Discord delivers DM message content to bots without `MESSAGE_CONTENT` (DMs are exempt from that restriction), and button clicks arrive as `INTERACTION_CREATE` which needs no intent at all. The bot's permissions are minimal: it DMs you, you DM it back, and it cannot read anything else.

## Daemon lifecycle

**Auto-started** by a `SessionStart` hook the moment a new Claude Code session opens (set `CC_DISCORD_AUTOSTART=off` to skip). If autostart is disabled or skipped, the daemon is still lazy-started the first time a hook or `/ask-discord` fires. Once up, it keeps running until you reboot or explicitly stop it. Check status:

```
/discord-status
```

Or directly:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/bin/discord-client.py" status
python3 "$CLAUDE_PLUGIN_ROOT/bin/discord-client.py" stop
```

(Where `$CLAUDE_PLUGIN_ROOT` is set automatically inside Claude Code; outside, the daemon lives at `~/.claude/plugins/cache/claude-code-discord-notifications/<version>/bin/`.)

The daemon writes a PID file at `~/.claude/discord-daemon.pid`, the info file at `~/.claude/discord-daemon.info`, and logs to `~/.claude/discord-daemon.log`.

## Requirements

- **Python 3.8+** on PATH as `python3`.
  - macOS / most Linux distros: already present.
  - Windows: install python.org Python (or use the one MSYS provides if it has pip). See **Windows notes** below.
- **`websockets`** and **`certifi`** packages — `/discord-setup` installs them via `pip install --user`. `certifi` is required on Windows because Python doesn't trust the OS cert store by default.
- A **Discord account** and a **server you can invite the bot to** (a private server-of-one works fine).

### Windows notes

The hook is a Python script (`hooks/notify.py`) invoked via `python3` in `hooks.json`. So whatever `python3` resolves to on your PATH must:

1. Be a working Python 3.8+ installation
2. Have `pip` available (or pip-installed `websockets` already)

The most reliable setup on Windows is:

1. Install Python from python.org (gives you `py` launcher with pip).
2. Make sure `python3` resolves to it. The easiest way: from cmd/PowerShell run

   ```
   echo @py -3 %%* > "%USERPROFILE%\AppData\Local\Microsoft\WindowsApps\python3.bat"
   ```

   That dir is on Windows PATH by default, so `python3` will now mean "Python 3 via the py launcher" in any new terminal.

3. Verify: `python3 -c "import websockets"` in a fresh shell.

4. Restart Claude Code so it picks up the new PATH.

If you only have MSYS Python (`/c/msys64/mingw64/bin/python3`), you can install pip into it via `pacman -S mingw-w64-x86_64-python-pip` from an MSYS terminal, then `pip install websockets`. The plugin will work either way.

## Customize

Variables you can set in the env file or shell:

- `CC_DISCORD_INCLUDE_DIR=0` — drop the working-directory line from messages
- `CC_DISCORD_DEBUG=1` — verbose logging to `~/.claude/cc-discord.log`

The `Stop` hook fires every turn, which can be chatty. To get pings only for questions / permissions / errors, edit `~/.claude/settings.json` and remove the `Stop` entry — or use `/hooks` to disable just that one.

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

Stop the daemon: `python3 ~/.claude/plugins/cache/claude-code-discord-notifications/*/bin/discord-client.py stop`. Delete credentials: `rm ~/.claude/.discord.env` (`del %USERPROFILE%\.claude\.discord.env` on Windows cmd).

## Files

```
.claude-plugin/
  plugin.json          plugin manifest
hooks/
  hooks.json           hook registrations (auto-applied)
  sessionstart.py      SessionStart — spawns the daemon at session open
  inbox.py             UserPromptSubmit — drains queued Discord DMs into context
  permission.py        PreToolUse — Discord approval for risky tools
  posttool.py          PostToolUse — accumulates per-turn tool summary
  notify.py            Stop / Notification / StopFailure — DMs the user
commands/
  discord-setup.md     /discord-setup wizard
  ask-discord.md       /ask-discord round-trip question
  discord-status.md    /discord-status daemon health
bin/
  discord-daemon.py    long-running gateway WebSocket client + TCP IPC server
  discord-client.py    CLI: talks to the daemon over TCP loopback
scripts/
  test_discord.py      credential test helper (used by /discord-setup)
```

## Troubleshooting

**"Stop hook error: Failed to run: EFTYPE: inappropriate file type or format" on Windows.** That was an old `.sh` hook — make sure you're on the latest version where the hook is `notify.py`.

**The daemon won't start.** Check `~/.claude/discord-daemon.log`. Most likely: `websockets` isn't installed, or `python3` doesn't resolve to a working interpreter, or the env file is missing/malformed.

**I tap a button but Claude doesn't continue.** The daemon should ACK the button click within 3 seconds. If the message gets edited to show your answer (and the buttons disappear), the daemon got it. If `/ask-discord` is still hanging, check pending count: `/discord-status`.

**No DM arrives.** Re-test credentials: `python3 "$CLAUDE_PLUGIN_ROOT/scripts/test_discord.py" <token> <user_id>`. Most common cause: bot doesn't share a server with you (step 5 of setup).

**`python3 -m pip` says "No module named pip" on MSYS.** Your MSYS Python has no pip. Either install pip via pacman (`pacman -S mingw-w64-x86_64-python-pip` from an MSYS terminal), or set up a `python3.bat` shim to use Windows Python (see **Windows notes** above).
