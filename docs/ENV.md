# RELAIS — Variables d'environnement

<!-- AUTO-GENERATED from .env.example — ne pas éditer manuellement -->
**Généré le:** 2026-03-28

Copier `.env.example` vers `.env` et renseigner les valeurs :

```bash
cp .env.example .env
```

---

## LLM Provider

| Variable | Requis | Description | Exemple |
|----------|--------|-------------|---------|
| `OPENROUTER_API_KEY` | Oui | Clé API OpenRouter (ou autre provider LiteLLM) | `sk-or-xxx` |

## Canaux de messagerie

| Variable | Requis | Description | Exemple |
|----------|--------|-------------|---------|
| `DISCORD_BOT_TOKEN` | Non* | Token bot Discord | `xxx` |
| `TELEGRAM_BOT_TOKEN` | Non* | Token bot Telegram | `xxx` |
| `SLACK_BOT_TOKEN` | Non* | Token bot Slack | `xoxb-xxx` |
| `SLACK_SIGNING_SECRET` | Non* | Secret de signature Slack | `xxx` |

*Au moins un canal doit être configuré pour recevoir des messages.

## Redis

| Variable | Requis | Description | Exemple |
|----------|--------|-------------|---------|
| `REDIS_SOCKET_PATH` | Non | Chemin du socket Unix Redis (défaut: `./.relais/redis.sock`) | `./.relais/redis.sock` |
| `REDIS_PASSWORD` | Non | Mot de passe Redis principal | `xxx` |

## Redis ACL par brique

Chaque brique a un mot de passe Redis séparé pour l'isolation de sécurité :

| Variable | Brique |
|----------|--------|
| `REDIS_PASS_AIGUILLEUR` | Relays (Discord, Telegram, Slack) |
| `REDIS_PASS_PORTAIL` | Portail |
| `REDIS_PASS_SENTINELLE` | Sentinelle |
| `REDIS_PASS_ATELIER` | Atelier (LLM caller) |
| `REDIS_PASS_SOUVENIR` | Souvenir (mémoire) |
| `REDIS_PASS_ARCHIVISTE` | Archiviste |
| `REDIS_PASS_SCHEDULER` | Scheduler (futur) |
| `REDIS_PASS_HERALD` | Herald (futur) |
| `REDIS_PASS_LEARNER` | Learner (futur) |
| `REDIS_PASS_WARDEN` | Warden (futur) |
| `REDIS_PASS_INTAKE` | Intake (futur) |
| `REDIS_PASS_INSPECTOR` | Inspector (futur) |
| `REDIS_PASS_WEAVER` | Weaver (futur) |

## LLM Provider (Anthropic direct)

| Variable | Requis | Description | Exemple |
|----------|--------|-------------|---------|
| `ANTHROPIC_API_KEY` | Oui | Clé API directe vers Anthropic (utilisée par DeepAgents/LangChain) | `sk-ant-xxx` |

## Chemins optionnels

| Variable | Requis | Description | Exemple |
|----------|--------|-------------|---------|
| `RELAIS_HOME` | Non | Répertoire de données RELAIS (défaut: `~/.relais`) | `/opt/relais` |
| `RELAIS_DB_PATH` | Non | Chemin SQLite pour Souvenir (défaut: `~/.relais/storage/memory.db`) | `/data/memory.db` |

---

**Voir aussi:** [ARCHITECTURE.md](ARCHITECTURE.md) — détails par brique
