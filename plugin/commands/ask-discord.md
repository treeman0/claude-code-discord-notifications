---
description: Ask a question on Discord and wait for the reply from your phone
argument-hint: "<question> [-- option1 | option2 | option3]"
allowed-tools: Bash($CLAUDE_PLUGIN_ROOT/bin/discord-client.py:*)
---

# Discord round-trip question

The user wants to ask a question on Discord and have the answer come back here. The arguments are: `$ARGUMENTS`

Parse the arguments:
- The question is everything before the literal token `--`. If there's no `--`, the whole arg string is the question.
- Options are pipe-separated after `--`. E.g. `Deploy now? -- yes | no | hold on` → question `"Deploy now?"`, options `["yes", "no", "hold on"]`.
- Trim whitespace from each option. Keep them short — Discord buttons cap at 80 chars and we cap at 25 options.

Then build a bash invocation that calls the discord-client, passing each option as `--option <text>`. Run it with the `!` prefix so the output lands in your context.

**Free-form** (no options, user types the answer):

```
!python3 "$CLAUDE_PLUGIN_ROOT/bin/discord-client.py" ask "What's the commit message?"
```

**With buttons:**

```
!python3 "$CLAUDE_PLUGIN_ROOT/bin/discord-client.py" ask "Deploy now?" --option "yes" --option "no" --option "hold on"
```

Default timeout is 600 seconds. For longer questions, append `--timeout 1800` for 30 minutes.

**Shell quoting:** Pass the question as a single double-quoted argument. Escape any inner double quotes as `\"`. Each option is its own `--option "..."` flag.

After the command runs, the answer (or `(no reply: timeout)` on stderr) will be in your context. Acknowledge what came back and act on it. If you got a timeout, ask the user directly in the chat what they want to do.

Do not send a separate notification before or after — the question itself reaches the user via Discord, and the answer is the round-trip.
