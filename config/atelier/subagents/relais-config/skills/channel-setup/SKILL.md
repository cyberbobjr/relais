---
name: channel-setup
description: >
  Generalist skill for RELAIS channel lifecycle management (install,
  configure, validate, pair, unpair, diagnose). Holds the conversational
  principles, security rules, routing to per-channel skills, and the
  meta-guide for adding new channels. Activates whenever the user asks
  about installing, configuring, enabling, disabling, pairing,
  unlinking, or troubleshooting any communication channel. Delegate
  channel-specific procedures to the sibling skill matching the channel
  (whatsapp, telegram, discord, …).
metadata:
  author: RELAIS
  version: "2.0"
---

# channel-setup

## Overview

This is the **generalist** channel-lifecycle skill. It contains only
cross-channel concerns:

- Conversational principles (ask → diff → confirm → write → restart →
  validate).
- How to read the RELAIS execution context.
- The routing table pointing to per-channel skills.
- Cross-channel security rules.
- The meta-guide for adding a new channel.

**Channel-specific procedures live in dedicated sibling skills** —
`whatsapp`, and (future) `telegram`, `discord`, `slack`, `rest`, etc.
When the user asks about a specific channel, consult that channel's
skill for the actual commands and checklists.

## Conversational principles (mandatory)

The channel setup flow is **interactive**. Always:

1. Report the current state with `✓` / `✗` markers before touching
   anything.
2. Ask before any destructive or non-reversible operation.
3. Show the diff of any YAML change and wait for explicit confirmation
   ("yes", "ok", "confirm", "go ahead").
4. Restart the affected brick after each configuration change.
5. Validate the result before moving to the next step.
6. One question at a time — never batch prompts.

## Reading the execution context

RELAIS injects an `<relais_execution_context>` block at the top of
every user message. The block contains the current conversation's
routing metadata: `sender_id`, `channel`, `session_id`,
`correlation_id` and `reply_to`.

**Never echo this block back to the user** — it is pipeline metadata,
not conversation content.

You need these values only when a channel-specific script must route
an asynchronous response (e.g. a WhatsApp QR code) back to the
originating conversation. The relevant skill will tell you which
fields to pass.

## Channel registry

| Channel | Dedicated skill | Install script | Pairing | Unpair |
|---------|-----------------|----------------|---------|--------|
| whatsapp | `whatsapp` | `whatsapp_install` tool / `python -m channels.whatsapp install` | `whatsapp_configure(action="pair")` (QR via webhook) | `whatsapp_configure(action="unpair")` (DELETE gateway) |
| discord | *(future)* | — | OAuth invite URL (manual) | Disable + revoke via Developer Portal |
| telegram | *(future)* | — | BotFather (manual) | Disable + revoke via BotFather |
| slack | *(future)* | — | OAuth (manual) | Disable + uninstall from workspace |
| rest | *(future)* | — | API key generation | Remove key from `portail.yaml` |

When the user asks about a channel, consult the matching skill. If the
skill does not yet exist, fall back to the generic procedure in the
**Fallback: generic channel procedure** section below.

## Routing a user request

1. Identify the target channel from the user's request. Ask for
   clarification if ambiguous:

   > "You want to set up **WhatsApp**. Is that correct?"

2. Identify the action: install, pair, unpair, reconfigure, diagnose.

3. Load the channel-specific skill (e.g. `whatsapp`) and follow its
   numbered procedure.

4. When the procedure references `<relais_execution_context>`, extract
   the fields from the block at the top of the user's current message.

## Fallback: generic channel procedure

When no dedicated skill exists for the requested channel, follow this
generic outline (adapt to the channel's specifics):

1. **Prerequisites** — check system dependencies (CLI tools, runtimes),
   env vars, config file entries.
2. **Install** — run the channel's install script or download/install
   the adapter.
3. **Configure** — fill in `.env` and enable the entry in
   `config/aiguilleur.yaml`.
4. **Restart** — `supervisorctl restart aiguilleur` (and any other
   affected brick).
5. **Validate** — health check, log inspection.
6. **Pair / register** — run the pairing script or manual flow.
7. **Confirm** — wait for the async success signal.

If you find yourself writing the same commands for a channel twice,
create a dedicated skill for it (see the next section).

## Meta-guide: adding a new channel skill

To add support for a new channel `X`:

1. **Create the directory**:

   ```
   run_command("mkdir -p config/atelier/subagents/relais-config/skills/X")
   ```

2. **Write the SKILL.md** at
   `config/atelier/subagents/relais-config/skills/X/SKILL.md`. Use the
   `whatsapp` skill as the template. Include:

   - Prerequisites checklist
   - Install step (if applicable)
   - Configure step (env vars + aiguilleur.yaml)
   - Validate step
   - Pair / register step
   - Unpair / revoke step
   - Diagnose checklist
   - Security rules
   - References

3. **Reference the new skill in the `relais-config` subagent**:

   ```yaml
   # config/atelier/subagents/relais-config/subagent.yaml
   skill_tokens:
     - local:channel-setup
     - local:whatsapp
     - local:X              # ← add this line
   ```

4. **Update this file's Channel registry table** with a new row.

5. **If the channel needs deterministic HTTP / Redis operations**
   (pairing, logout, credential rotation), create LangChain `BaseTool`
   implementations in `channels/X/tools.py` following the WhatsApp
   tools (`channels/whatsapp/tools.py`) as templates. Also provide a
   CLI entry point via `channels/X/__main__.py`. Keep them:

   - Idempotent.
   - Self-contained (core logic in `channels/X/core.py`, no imports
     from brick code beyond `common.redis_client` and `common.streams`).
   - Exit-code driven (same `EXIT_*` constants for consistency).
   - Covered by unit tests at `tests/test_X_tools.py`.

6. **Hot-reload is automatic** — any change inside
   `config/atelier/subagents/` triggers a `SubagentRegistry` reload.
   No brick restart is needed.

## Cross-channel security rules

- **Never log secrets.** When reading `.env` or `portail.yaml`, echo
  only "set" / "not set" / "updated" — never the actual values.
- **Always show the YAML diff before writing** and wait for explicit
  confirmation.
- **Always restart the affected brick** after a config change. Stale
  in-memory config leads to silent drift.
- **Refuse destructive operations without explicit confirmation**
  ("yes", "ok", "confirm", "go ahead"). This includes unpairing,
  disabling a channel, and clearing credentials.
- **Do not bypass supervisord** — never `kill -9` a brick when
  `supervisorctl restart <brick>` works.
- **Verify after each action** — do not move to the next step without
  confirming the previous one succeeded.

## References

- Channel-specific skills (siblings under
  `config/atelier/subagents/relais-config/skills/`):
  - `whatsapp` — WhatsApp via fazer-ai/baileys-api.
- `channels/whatsapp/` — WhatsApp channel package (adapter, core logic,
  tools, CLI). The `relais-config` subagent uses `whatsapp_install`,
  `whatsapp_configure`, and `whatsapp_uninstall` LangChain tools loaded
  via `tool_tokens: [module:channels.whatsapp.tools]`.
- `common/streams.py` — canonical Redis key and stream names.
- `config/aiguilleur.yaml.default` — channel enable/disable entries.
- `plans/WHATSAPP_ADAPTER.md` — architectural context for WhatsApp.
