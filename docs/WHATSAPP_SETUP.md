# WhatsApp Setup Guide

This guide covers how to set up the WhatsApp channel adapter for RELAIS using the Baileys gateway.

## Prerequisites

- **Bun** runtime (`curl -fsSL https://bun.sh/install | bash`)
- A **WhatsApp account** with a phone number
- RELAIS running with Redis configured

## Installation

### 1. Install baileys-api

Run the automated install script:

```bash
./scripts/install_whatsapp.sh
```

This clones [fazer-ai/baileys-api](https://github.com/fazer-ai/baileys-api) to `$RELAIS_HOME/vendor/baileys-api` and runs `bun install`.

### 2. Create an API key

```bash
cd $RELAIS_HOME/vendor/baileys-api
bun scripts/manage-api-keys.ts create user relais-adapter
```

Store the output key as `WHATSAPP_API_KEY` in your `.env` file.

### 3. Configure environment variables

Add to `.env`:

```bash
WHATSAPP_GATEWAY_URL=http://localhost:3025
WHATSAPP_PHONE_NUMBER=+33612345678        # your phone number in E.164 format
WHATSAPP_API_KEY=<key from step 2>
WHATSAPP_WEBHOOK_SECRET=<random string, min 6 chars>
WHATSAPP_WEBHOOK_PORT=8765
WHATSAPP_WEBHOOK_HOST=127.0.0.1
REDIS_PASS_BAILEYS=pass_baileys
```

### 4. Configure Redis

The RELAIS Redis is configured in `config/redis.conf`. TCP port 6379 is enabled by default (bound to localhost only) for the baileys-api connection. The `baileys` ACL user is pre-configured with access restricted to the `@baileys-api:` keyspace.

### 5. Map owner identity in portail.yaml

Add your phone number under `identifiers.whatsapp.self` for the admin user:

```yaml
usr_admin:
  display_name: "Admin"
  role: admin
  identifiers:
    whatsapp:
      self: "+33612345678"   # the bot's own number
    discord:
      dm: "123456789"
```

**Recommended**: set `unknown_user_policy: deny` in `portail.yaml` so RELAIS does not respond to unknown contacts.

### 6. Enable WhatsApp in aiguilleur.yaml

Edit `~/.relais/config/aiguilleur.yaml`:

```yaml
channels:
  whatsapp:
    enabled: true
    streaming: false
    profile: default
    prompt_path: "channels/whatsapp_default.md"
```

### 7. Start services

```bash
# Start baileys-api (manual — not auto-started)
supervisorctl start optional:baileys-api

# Restart Aiguilleur to pick up the new channel
supervisorctl restart relays:aiguilleur
```

## Pairing via QR Code

1. Type `/settings whatsapp` on any connected channel (Discord, Telegram, etc.)
2. An ASCII QR code will appear in the channel
3. Open **WhatsApp > Settings > Linked Devices > Link a Device**
4. Scan the QR code
5. A confirmation message appears when linked

Session credentials are stored in Redis by baileys-api and survive restarts.

## How It Works

### Owner identity model

- `identifiers.whatsapp.self` = "I am the owner of this phone number" (self-chat = admin talking to RELAIS)
- `identifiers.whatsapp.dm` = "someone contacts me via this number" (external contacts)

### Message routing

- **Self-chat** (note to self): admin sends a message in their own chat → RELAIS responds
- **External DM**: contact sends a message → RELAIS responds based on their portail.yaml role
- **Admin replies manually** in external conversations (`fromMe: true`): RELAIS ignores (no interference)
- **Group messages**: filtered by the adapter (DM only in MVP)

### Adding contacts

Manually add contacts to `portail.yaml`:

```yaml
usr_guest:
  display_name: "Pierre"
  role: guest
  identifiers:
    whatsapp:
      dm: "+33699999999"
```

## Networking

The webhook host (`WHATSAPP_WEBHOOK_HOST`) is used as both:
- The bind address for the adapter's webhook server
- The hostname in the `webhookUrl` sent to the gateway

**This only works when gateway and adapter run in the same network namespace.** For Docker deployments where baileys-api runs in a container:
- macOS/Windows: use `host.docker.internal`
- Linux: use `--network=host`

## Security

- The `webhookVerifyToken` is a shared secret (NOT HMAC signature) — sufficient for localhost-only deployments
- All mutation requests to baileys-api include the `x-api-key` header
- The Redis `baileys` user has access restricted to the `@baileys-api:` keyspace

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Gateway not reachable | Check `curl http://localhost:3025/status` — start with `supervisorctl start optional:baileys-api` |
| QR code not appearing | Verify adapter is running (`/health` endpoint), check pairing TTL (5min) |
| Session expired | Re-run `/settings whatsapp` to re-pair |
| `fromMe` loop | Normal — adapter tracks sent messages in `sent_message_ids` |
| Wrong phone number error | Check `WHATSAPP_PHONE_NUMBER` matches your actual number |
| Adapter not found | Enable `whatsapp: enabled: true` in `aiguilleur.yaml` and restart Aiguilleur |

## Known MVP Limitations

- **In-memory deduplication**: `seen_message_ids` and `sent_message_ids` do not survive adapter restarts. Webhook retries after restart may produce duplicates. Future improvement: Redis-backed dedup with SETNX + TTL.
- **Text only**: non-text messages (images, audio, video) are skipped.
- **No streaming**: baileys-api does not support token-by-token streaming.
- **No typing indicator**: WhatsApp typing indicators not implemented in MVP.
