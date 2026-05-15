---
description: Check the status of the Discord notification daemon
allowed-tools: Bash($CLAUDE_PLUGIN_ROOT/bin/discord-client.py:*), Bash(tail:*)
---

Check the daemon status and report it back briefly.

Run:

```
!python3 "$CLAUDE_PLUGIN_ROOT/bin/discord-client.py" status
```

If it says "not running" and the user expects it to be running, suggest sending any message (which triggers the hook → lazy-start), or running `/ask-discord ping -- ok` to force-start it.

If it's running but `pending > 0`, that means a question is open waiting for a reply on Discord — mention it.

You can also tail the last few lines of the daemon log if useful:

```
!tail -20 ~/.claude/discord-daemon.log
```

Keep the response short.
