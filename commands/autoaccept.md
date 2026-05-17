---
description: Toggle Discord auto-accept (all tool-permission prompts auto-approve)
argument-hint: "[on | off | status]"
allowed-tools: Bash($CLAUDE_PLUGIN_ROOT/bin/discord-client.py:*)
---

Toggle (or query) the cc-discord auto-accept flag. When ON, the PreToolUse hook short-circuits to "allow" for every tool except `AskUserQuestion` — no Discord DM, no terminal prompt, the tool just runs. The same flag is set by the `/autoaccept` DM command on Discord, so the terminal and phone stay in sync.

Argument: `$ARGUMENTS`

- No argument → toggle the current state.
- `on` → enable.
- `off` → disable.
- `status` → report the current state without changing it.

Run the corresponding command:

```
!python3 "$CLAUDE_PLUGIN_ROOT/bin/discord-client.py" autoaccept $ARGUMENTS
```

After it runs, the last line of output is `auto-accept is ON` or `auto-accept is OFF`. Echo that back to the user in one short sentence — no extra commentary.

The flag persists across sessions and across daemon restarts (stored at `~/.claude/cc-discord/auto-accept.json`).

**Safety note**: with auto-accept ON, every Bash/Edit/Write/MultiEdit call is allowed without prompting. Remind the user once if they're enabling it.
