---
name: whatsapp
description: >
  Installs, configures, pairs, unpairs, and diagnoses the WhatsApp
  channel using the whatsapp_install, whatsapp_configure, and
  whatsapp_uninstall tools. Activates when the user mentions WhatsApp
  in a setup, pairing, logout, or troubleshooting context.
metadata:
  author: RELAIS
  version: "2.0"
---

# whatsapp

## Tools

- **whatsapp_install** — one-call install (vendor + API key + config + services)
- **whatsapp_configure** — action-based: pair, unpair, health, status, enable, disable, set_env
- **whatsapp_uninstall** — reverse of install (stop + disable + optional cleanup)

## Happy path: fresh install

1. Call `whatsapp_install(phone_number="+33...", webhook_secret="<random 16+ chars>")`
2. Call `whatsapp_configure(action="health")` to verify
3. Extract routing metadata from `<relais_execution_context>`
4. Call `whatsapp_configure(action="pair", params={sender_id, channel, session_id, correlation_id, reply_to})`
5. Tell user to scan QR in WhatsApp > Settings > Linked Devices

## Unpair

1. Confirm intent with the user (destructive)
2. Call `whatsapp_configure(action="unpair")`

## Diagnose

1. `whatsapp_configure(action="health")` — adapter + gateway check
2. `whatsapp_configure(action="status")` — full status report

## Uninstall

1. Confirm with the user
2. Call `whatsapp_uninstall(clean_vendor=True, clean_env=True)` for full cleanup

## Security

- Never log secrets. Tools echo "set" / "not set" only.
- Refuse pairing without a valid `<relais_execution_context>` block.
- Refuse unpairing without explicit user confirmation.
