# RELAIS — Environment Variables

This page lists the environment variables actually used by the current runtime of this repository.

---

## Main Variables

| Variable | Required | Actual Usage |
|----------|----------|--------------|
| `RELAIS_HOME` | No | RELAIS working directory. Default: `./.relais` at the repo root. |
| `LOG_LEVEL` | No | Python log level (`INFO` by default in the bricks). |
| `REDIS_SOCKET_PATH` | No | Redis Unix socket path. Default: `<RELAIS_HOME>/redis.sock`. |
| `REDIS_PASSWORD` | No | Generic Redis password fallback if no brick-specific password is provided. |
| `RELAIS_DB_PATH` | No | Override for the SQLite path used by Alembic for `memory.db`. |

---

## Redis ACL per Brick

Each `RedisClient("<brick>")` first looks for `REDIS_PASS_<BRICK>`, then falls back to `REDIS_PASSWORD`.

| Variable | Used by |
|----------|---------|
| `REDIS_PASS_AIGUILLEUR` | `aiguilleur` |
| `REDIS_PASS_PORTAIL` | `portail` |
| `REDIS_PASS_SENTINELLE` | `sentinelle` |
| `REDIS_PASS_ATELIER` | `atelier` |
| `REDIS_PASS_SOUVENIR` | `souvenir` |
| `REDIS_PASS_COMMANDANT` | `commandant` |
| `REDIS_PASS_ARCHIVISTE` | `archiviste` |
| `REDIS_PASS_FORGERON` | `forgeron` (autonomous skill improvement) |
| `REDIS_PASS_BAILEYS` | external `baileys-api` gateway (WhatsApp) — used by the supervisord program `baileys-api`, not by a Python brick |

---

## LLM Providers

| Variable | Required | Actual Usage |
|----------|----------|--------------|
| `ANTHROPIC_API_KEY` | Often yes | Used by Anthropic profiles in `config/atelier/profiles.yaml`. |
| `OPENROUTER_API_KEY` | Optional | Used if an OpenRouter profile is configured. |

`atelier.profile_loader` reads the variable name from `api_key_env` in `profiles.yaml`. Any other variable can therefore be used if explicitly referenced in a profile.

---

## Channels

| Variable | Required | Actual Usage |
|----------|----------|--------------|
| `DISCORD_BOT_TOKEN` | Yes for Discord | Read by the Discord adapter. |
| `TELEGRAM_BOT_TOKEN` | Optional | Expected if a Telegram adapter is enabled. |
| `SLACK_BOT_TOKEN` | Optional | Present in the env template, but not used by a full adapter in this repository. |
| `SLACK_SIGNING_SECRET` | Optional | Same note as above. |

### REST Channel

Only required when `rest.enabled: true` in `aiguilleur.yaml`. The adapter exposes `POST /v1/messages` with Bearer authentication. API keys are resolved via `UserRegistry.resolve_rest_api_key()` (HMAC-SHA256, never stored in plaintext).

| Variable | Required | Actual Usage |
|----------|----------|--------------|
| `RELAIS_API_KEY_SALT` | Recommended | Salt used by `portail/user_registry.py` to hash REST API keys with HMAC-SHA256. If absent, an empty salt is used with a WARNING at portail startup. Any value is acceptable — keep it secret. |

---

### WhatsApp Channel (`baileys-api` gateway)

Only required when `whatsapp.enabled: true` in `aiguilleur.yaml`. The Python adapter (`aiguilleur/channels/whatsapp/adapter.py`) communicates with the external [fazer-ai/baileys-api](https://github.com/fazer-ai/baileys-api) gateway launched by supervisord (program `baileys-api`, group `optional`, autostart disabled). See [docs/WHATSAPP_SETUP.md](WHATSAPP_SETUP.md) for the full setup procedure.

| Variable | Required | Actual Usage |
|----------|----------|--------------|
| `WHATSAPP_GATEWAY_URL` | Yes | HTTP URL of the baileys-api gateway (default: `http://localhost:3025`). |
| `WHATSAPP_PHONE_NUMBER` | Yes | Bot phone number in E.164 format (e.g. `+33612345678`). |
| `WHATSAPP_API_KEY` | Yes | API key generated on the gateway side via `bun scripts/manage-api-keys.ts create user relais`. |
| `WHATSAPP_WEBHOOK_SECRET` | Yes | Shared secret between the gateway and the adapter to authenticate incoming webhooks. |
| `WHATSAPP_WEBHOOK_PORT` | No | Listening port for the adapter's aiohttp webhook server (default: `8765`). |
| `WHATSAPP_WEBHOOK_HOST` | No | Listening host AND callback URL passed to the gateway. Default: `127.0.0.1` (co-location required). |
| `REDIS_PASS_BAILEYS` | Yes | Redis password for the `baileys` ACL user (see `config/redis.conf`). Consumed by supervisord to build the `REDIS_URL` for the `baileys-api` program. |

Optional Python dependencies to install: `uv sync --extra whatsapp` (adds `qrcode>=8.2`).

---

## Debug

These variables are read by [launcher.py](../launcher.py):

| Variable | Required | Actual Usage |
|----------|----------|--------------|
| `DEBUGPY_ENABLED` | No | Enables `debugpy` if set to `1`. |
| `DEBUGPY_PORT` | No | debugpy listening port. |
| `DEBUGPY_WAIT` | No | Waits for a debugger to attach if set to `1`. |

---

## Variables Used in MCP Examples

Some values are not needed by the core runtime, but become useful if you enable the example MCP servers from the [config/atelier/mcp_servers.yaml.default](../config/atelier/mcp_servers.yaml.default) template.

| Variable | Usage |
|----------|-------|
| `GITHUB_TOKEN` | Example GitHub MCP server |
| `BRAVE_API_KEY` | Example Brave Search MCP server |

---

## Important Notes

- The current RELAIS runtime does not read Redis connection config from `config/config.yaml`. Bricks primarily use `REDIS_SOCKET_PATH`, `REDIS_PASS_<BRICK>`, and `REDIS_PASSWORD`.
- `RELAIS_HOME` also drives resolution of the `prompts`, `skills`, `logs`, `media`, and `storage` directories.
- `storage/memory.db` is initialized automatically at souvenir startup via `SQLModel.metadata.create_all`.
