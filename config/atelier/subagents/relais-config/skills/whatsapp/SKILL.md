---
name: whatsapp
description: >
  Installs, configures, pairs, unpairs and diagnoses the WhatsApp
  channel adapter (fazer-ai/baileys-api gateway + RELAIS
  WhatsAppAiguilleur). Activates when the user mentions WhatsApp in a
  setup, installation, pairing, logout, disconnection, or
  troubleshooting context. Use alongside the generalist
  ``channel-setup`` skill which holds the cross-channel principles.
metadata:
  author: RELAIS
  version: "1.0"
---

# whatsapp

## Overview

This skill walks the RELAIS operator through the complete lifecycle of
the WhatsApp channel:

1. **Install** the Baileys gateway (fazer-ai/baileys-api).
2. **Configure** environment variables and `aiguilleur.yaml`.
3. **Validate** that the adapter webhook is healthy.
4. **Pair** the bot's phone number by scanning a QR code.
5. **Unpair** (logout) when the user wants to decommission.
6. **Diagnose** failures at any step.

Always read the generalist ``channel-setup`` skill first for the
conversational principles (ask → diff → confirm → write → restart →
validate) and for the ``<relais_execution_context>`` block that
provides routing metadata for the pairing step.

## Prerequisites (WhatsApp-specific)

Run this checklist before touching any file. Report each item as `✓`
or `✗` and stop to wait for user input before making changes.

1. **`bun` runtime** — `run_command("command -v bun")`. If missing,
   the user must install it first:
   `curl -fsSL https://bun.sh/install | bash`
   Stop here if bun is absent.
2. **baileys-api installed** — check
   `run_command("ls $RELAIS_HOME/vendor/baileys-api/package.json 2>/dev/null && echo OK")`.
3. **Env vars in `.env`** — read `.env` and check that
   `WHATSAPP_PHONE_NUMBER`, `WHATSAPP_API_KEY` and
   `WHATSAPP_WEBHOOK_SECRET` are set and non-empty.
4. **`aiguilleur.yaml`** — read the file and report the current value
   of `whatsapp.enabled`.
5. **Adapter webhook** — `run_command("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/health || true")`.
6. **baileys-api process** —
   `run_command("supervisorctl status optional:baileys-api")`.

## 1. Install

If baileys-api is not installed, propose to run the installer. The
script is idempotent and safe to re-run.

```
run_command("bash scripts/install_whatsapp.sh")
```

Show the user the script name before running it and wait for
confirmation.

## 2. Configure environment variables

For each missing env var, ask one question at a time:

- **`WHATSAPP_PHONE_NUMBER`**: "What is the bot's WhatsApp phone
  number in international format (e.g. `+33612345678`)?"
- **`WHATSAPP_API_KEY`**: "Have you generated an API key yet? If not,
  run `cd $RELAIS_HOME/vendor/baileys-api && bun
  scripts/manage-api-keys.ts create user relais-adapter` and paste the
  output."
- **`WHATSAPP_WEBHOOK_SECRET`**: "Choose a random secret of at least
  16 characters — it authenticates webhook calls from baileys-api to
  the adapter."

Append / update `.env` via `write_file`. **Never log the values
themselves** — echo only "set" / "not set" / "updated".

## 3. Enable the channel in aiguilleur.yaml

1. Read `config/aiguilleur.yaml`.
2. Find the `whatsapp:` entry and set `enabled: true`.
3. Show the diff and wait for confirmation.
4. Write the file.
5. Restart the aiguilleur brick:
   `run_command("supervisorctl restart aiguilleur")`.

## 4. Start the baileys-api gateway

```
run_command("supervisorctl start optional:baileys-api")
run_command("supervisorctl status optional:baileys-api")
```

## 5. Validate the adapter webhook

```
run_command("curl -s -f http://127.0.0.1:8765/health && echo OK")
```

On failure:

```
run_command("supervisorctl tail aiguilleur stderr")
```

Explain the error to the user in plain language.

## 6. Pair (QR code)

Extract `sender_id`, `channel`, `session_id`, `correlation_id` and
`reply_to` from the `<relais_execution_context>` block at the top of
the user's current message. These values route the QR code back to
**this conversation**.

```bash
run_command("python scripts/pair_whatsapp.py \
  --sender-id '<SENDER_ID>' \
  --channel '<CHANNEL>' \
  --session-id '<SESSION_ID>' \
  --correlation-id '<CORRELATION_ID>' \
  --reply-to '<REPLY_TO>'")
```

**Always quote the values** — channel-specific IDs may contain
special characters.

### Pairing exit codes

| Exit | Meaning | What to tell the user |
|------|---------|-----------------------|
| 0 | Pairing initiated | "Open WhatsApp > Settings > Linked Devices > Link a Device. The QR code will appear here in a moment." |
| 1 | Bad args / missing env var | Re-check step 2. |
| 2 | Adapter webhook unreachable | Re-check step 3 (aiguilleur restart) and step 5 (/health). |
| 3 | Gateway POST failed | Re-check step 4 (baileys-api running). |
| 4 | Redis write failed | Check `REDIS_PASS_COMMANDANT` and Redis liveness. |

### Confirming the pair

The adapter pushes a "WhatsApp successfully linked!" message
asynchronously when the user scans the QR. Tell the user to watch for
it. If the QR never appears within 60 seconds, jump to the **Diagnose**
section.

**Refuse to run pairing** if the execution context block is missing or
has empty values — ask the user to re-send the request.

## 7. Unpair (logout)

Unpairing is destructive: after logout the user must re-scan a new QR
to reconnect.

1. **Confirm intent.** Ask: "Are you sure you want to unlink WhatsApp?
   This removes the device from WhatsApp > Settings > Linked Devices.
   To reconnect you will need to scan a new QR code."
2. **Run the unpair script** — no routing metadata is required because
   the logout does not return an async message to the chat.

   ```
   run_command("python scripts/unpair_whatsapp.py")
   ```

   The script is idempotent: re-running it on an already-disconnected
   number is a no-op (HTTP 404 from the gateway is treated as success).

### Unpair exit codes

| Exit | Meaning | What to tell the user |
|------|---------|-----------------------|
| 0 | Logout successful (or already disconnected) | "WhatsApp unlinked. The device no longer appears in Linked Devices." |
| 1 | Missing `WHATSAPP_PHONE_NUMBER` or `WHATSAPP_API_KEY` | Re-check `.env`. |
| 3 | Gateway DELETE failed | Verify baileys-api is running. |
| 4 | Redis cleanup failed | Check `REDIS_PASS_COMMANDANT` and Redis liveness. |

### Follow-up actions (ask one at a time)

- **Disable the channel in `aiguilleur.yaml`?** — If yes, read the
  file, set `whatsapp.enabled: false`, show the diff, wait for
  confirmation, write, then
  `run_command("supervisorctl restart aiguilleur")`.
- **Stop the baileys-api gateway?** — If yes:
  `run_command("supervisorctl stop optional:baileys-api")`.
- **Clear `.env` credentials?** — If the user wants a full
  decommission, propose to remove `WHATSAPP_API_KEY`,
  `WHATSAPP_WEBHOOK_SECRET` and `WHATSAPP_PHONE_NUMBER` from `.env`.
  Always ask first — they may want to keep them for a later re-pair.

## 8. Diagnose

When a step fails or the user reports unexpected behaviour, run this
checklist in order and report each result:

1. **Adapter logs** —
   `run_command("supervisorctl tail aiguilleur stderr")`
2. **Gateway logs** —
   `run_command("supervisorctl tail optional:baileys-api stderr")`
3. **Adapter health** —
   `run_command("curl -s -f http://127.0.0.1:8765/health && echo OK")`
4. **Gateway status** —
   `run_command("curl -s $WHATSAPP_GATEWAY_URL/status")` (no auth
   required on `/status`)
5. **Pairing context** —
   `run_command("redis-cli -s $REDIS_SOCKET_PATH GET relais:whatsapp:pairing")`
6. **Raw DELETE reproduction** (if logout fails) —
   `run_command("curl -s -X DELETE -H 'x-api-key: $WHATSAPP_API_KEY' $WHATSAPP_GATEWAY_URL/connections/$WHATSAPP_PHONE_NUMBER")`
7. **Env vars present** — read `.env` and confirm the three required
   WhatsApp variables are non-empty (echo only "set" / "not set",
   never the values).

Present findings as a numbered list with `✓` / `✗` markers and propose
a concrete next step based on the first failure found.

## Security rules

- **Never log secrets.** When reading `.env`, echo only set/not-set
  status.
- **Always show the YAML diff before writing** and wait for
  confirmation.
- **Always restart the affected brick** after a config change.
- **Refuse to run pairing without a valid execution context block**.
- **Refuse to run unpairing without explicit user confirmation**
  ("yes", "ok", "confirm", "go ahead").

## References

- `scripts/install_whatsapp.sh` — baileys-api installer.
- `scripts/pair_whatsapp.py` — deterministic pairing (POST gateway +
  SET Redis context).
- `scripts/unpair_whatsapp.py` — deterministic logout (DELETE gateway
  + DEL Redis context).
- `aiguilleur/channels/whatsapp/adapter.py` — webhook consumer that
  reads `relais:whatsapp:pairing` from Redis.
- `plans/WHATSAPP_ADAPTER.md` — architectural context (shared bot
  number model, Redis ACL, webhook security).
- Generalist sibling skill: `channel-setup` — cross-channel principles
  and extension guide.
