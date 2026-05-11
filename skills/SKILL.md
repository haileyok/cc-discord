---
name: ask-discord
description: Ask the user a question via Discord and wait for their reply. Use when blocked >5 minutes on a decision and the user is away from the keyboard. Times out gracefully.
---

# Ask the user via Discord

Use this when you are blocked on a decision and the user is plausibly away from the workstation. The bridge daemon (`cc-bridge`) posts the question to a Discord thread tied to the current session and waits up to 15 minutes for a reply. The user's reply (or a graceful fallback string on timeout / bridge-down) is returned to you to use as conversation context.

## When to use

- You've been working autonomously and hit a fork in the road that materially changes the result.
- The decision is hard to undo (data deletion, schema change, deploy, irreversible refactor).
- A code-only check (test runs, type checks) cannot answer the question.
- You'd otherwise wait or guess for >5 minutes.

## When NOT to use

- The answer is in the codebase or git history — read it.
- The decision is trivial / reversible — just pick one and proceed.
- The user is at the keyboard (recent activity in this session) — the normal turn-by-turn dialog is faster.

## How to invoke

Run the bridge's CLI script via the Bash tool. Pass your session ID (you received it in the session-start system reminder; the skill explicitly authorizes you to pass it to this script) and the current working directory:

```bash
python3 /home/discord/cc-discord/skills/ask_discord.py \
    "<the question>" \
    --session-id "<your session id>" \
    --cwd "$(pwd)"
```

The script blocks until the user replies in Discord, the bridge times out, or the bridge is unreachable. It always exits 0 and always writes ONE human-readable line to stdout. Use that line as the user's input to the next step of your reasoning.

Examples of return strings:
- `yes` — straight answer.
- `option B because C` — answer with rationale.
- `no reply within 15m; proceeding with best-guess` — timeout fallback. Make a reasonable call and continue.
- `bridge daemon is not reachable at http://127.0.0.1:8787; is \`cc-bridge serve\` running?` — bridge-down. Make a reasonable call and continue. Mention the bridge issue to the user when they return.

## Tone of the question

Be specific. The user is glancing at Discord on their phone. "Should I use option A or B?" — name A and B in the question itself. Two sentences max, plus a one-line "current cwd: …" so they have context.
