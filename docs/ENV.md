# RELAIS — Variables d'environnement

Cette page liste les variables réellement utiles au runtime actuel du dépôt.

---

## Variables principales

| Variable | Requis | Utilisation réelle |
|----------|--------|--------------------|
| `RELAIS_HOME` | Non | Répertoire de travail RELAIS. Défaut : `./.relais` à la racine du repo. |
| `LOG_LEVEL` | Non | Niveau de logs Python (`INFO` par défaut dans les briques). |
| `REDIS_SOCKET_PATH` | Non | Chemin du socket Unix Redis. Défaut : `<RELAIS_HOME>/redis.sock`. |
| `REDIS_PASSWORD` | Non | Mot de passe Redis générique de fallback si un mot de passe dédié à la brique n'est pas fourni. |
| `RELAIS_DB_PATH` | Non | Override du chemin SQLite utilisé par Alembic pour `memory.db`. |

---

## Redis ACL par brique

Chaque `RedisClient("<brick>")` cherche d'abord `REDIS_PASS_<BRICK>`, puis retombe sur `REDIS_PASSWORD`.

| Variable | Utilisée par |
|----------|--------------|
| `REDIS_PASS_AIGUILLEUR` | `aiguilleur` |
| `REDIS_PASS_PORTAIL` | `portail` |
| `REDIS_PASS_SENTINELLE` | `sentinelle` |
| `REDIS_PASS_ATELIER` | `atelier` |
| `REDIS_PASS_SOUVENIR` | `souvenir` |
| `REDIS_PASS_COMMANDANT` | `commandant` |
| `REDIS_PASS_ARCHIVISTE` | `archiviste` |
| `REDIS_PASS_BAILEYS` | passerelle externe `baileys-api` (WhatsApp) — utilisée par le programme supervisord `baileys-api`, pas par une brique Python |

Les autres mots de passe présents dans `.env.example` correspondent à des briques futures ou absentes de ce dépôt.

---

## Providers LLM

| Variable | Requis | Utilisation réelle |
|----------|--------|--------------------|
| `ANTHROPIC_API_KEY` | Souvent oui | Utilisée par les profils Anthropic dans `config/atelier/profiles.yaml`. |
| `OPENROUTER_API_KEY` | Optionnel | Utilisée si un profil OpenRouter est configuré. |

`atelier.profile_loader` lit le nom de variable depuis `api_key_env` dans `profiles.yaml`. Toute autre variable peut donc être utilisée si elle est référencée explicitement dans un profil.

---

## Canaux

| Variable | Requis | Utilisation réelle |
|----------|--------|--------------------|
| `DISCORD_BOT_TOKEN` | Oui pour Discord | Lue par l'adaptateur Discord. |
| `TELEGRAM_BOT_TOKEN` | Optionnel | Prévue si un adaptateur Telegram est activé. |
| `SLACK_BOT_TOKEN` | Optionnel | Présente dans le template d'env, mais pas utilisée par un adaptateur complet dans ce dépôt. |
| `SLACK_SIGNING_SECRET` | Optionnel | Même remarque que ci-dessus. |

### Canal WhatsApp (passerelle `baileys-api`)

Nécessaires uniquement quand `whatsapp.enabled: true` dans `aiguilleur.yaml`. L'adaptateur Python (`aiguilleur/channels/whatsapp/adapter.py`) parle à la passerelle externe [fazer-ai/baileys-api](https://github.com/fazer-ai/baileys-api) lancée par supervisord (programme `baileys-api`, groupe `optional`, autostart désactivé). Voir [docs/WHATSAPP_SETUP.md](/Users/benjaminmarchand/IdeaProjects/relais/docs/WHATSAPP_SETUP.md) pour la procédure complète.

| Variable | Requis | Utilisation réelle |
|----------|--------|--------------------|
| `WHATSAPP_GATEWAY_URL` | Oui | URL HTTP de la passerelle baileys-api (défaut : `http://localhost:3025`). |
| `WHATSAPP_PHONE_NUMBER` | Oui | Numéro de téléphone du bot au format E.164 (ex. `+33612345678`). |
| `WHATSAPP_API_KEY` | Oui | Clé API générée côté passerelle via `bun scripts/manage-api-keys.ts create user relais`. |
| `WHATSAPP_WEBHOOK_SECRET` | Oui | Secret partagé entre la passerelle et l'adaptateur pour authentifier les webhooks entrants. |
| `WHATSAPP_WEBHOOK_PORT` | Non | Port d'écoute du serveur webhook aiohttp de l'adaptateur (défaut : `8765`). |
| `WHATSAPP_WEBHOOK_HOST` | Non | Hôte d'écoute ET URL de callback passée à la passerelle. Défaut : `127.0.0.1` (colocation obligatoire). |
| `REDIS_PASS_BAILEYS` | Oui | Mot de passe Redis de l'utilisateur ACL `baileys` (voir `config/redis.conf`). Consommé par supervisord pour construire `REDIS_URL` du programme `baileys-api`. |

Dépendances optionnelles Python à installer en plus : `uv sync --extra whatsapp` (ajoute `aiohttp>=3.9` et `qrcode>=7.0`).

---

## Debug

Ces variables sont lues par [launcher.py](/Users/benjaminmarchand/IdeaProjects/relais/launcher.py) :

| Variable | Requis | Utilisation réelle |
|----------|--------|--------------------|
| `DEBUGPY_ENABLED` | Non | Active `debugpy` si vaut `1`. |
| `DEBUGPY_PORT` | Non | Port d'écoute debugpy. |
| `DEBUGPY_WAIT` | Non | Attend l'attachement d'un débogueur si vaut `1`. |

---

## Variables utiles aux exemples MCP

Certaines valeurs ne sont pas nécessaires au cœur du runtime, mais deviennent utiles si vous activez les serveurs MCP d'exemple du template [config/atelier/mcp_servers.yaml.default](/Users/benjaminmarchand/IdeaProjects/relais/config/atelier/mcp_servers.yaml.default).

| Variable | Utilisation |
|----------|-------------|
| `GITHUB_TOKEN` | Exemple de serveur MCP GitHub |
| `BRAVE_API_KEY` | Exemple de serveur MCP Brave Search |

---

## Remarques importantes

- Le runtime actuel de RELAIS ne lit pas la configuration Redis depuis `config/config.yaml` pour se connecter à Redis. Les briques utilisent surtout `REDIS_SOCKET_PATH`, `REDIS_PASS_<BRICK>` et `REDIS_PASSWORD`.
- `RELAIS_HOME` pilote aussi la résolution des répertoires `prompts`, `skills`, `logs`, `media` et `storage`.
- Pour initialiser `storage/memory.db`, utilisez `alembic upgrade head`.
