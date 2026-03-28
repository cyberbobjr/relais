# RELAIS

RELAIS est une architecture micro-brique pour un assistant IA autonome et modulaire. Chaque brique gère une responsabilité spécifique et communique via Redis Streams, permettant un système flexible, résilient et facilement extensible.

**État:** Phases 1, 2 et 3 implémentées. MVP core loop opérationnel.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      RELAIS MICRO-BRICK PIPELINE                             │
└─────────────────────────────────────────────────────────────────────────────┘

[External Channels]
      │
      ├──► Discord/Telegram/Slack (Aiguilleur Inbound)
      │
      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ PORTAIL (Gatekeeper - Consumer)                                              │
│ ├─ Consomme: relais:messages:incoming:{channel}                             │
│ ├─ Valide: schema, format                                                    │
│ ├─ Filtre: reply_policy (DND, vacation, in_meeting)                         │
│ └─ Publie: relais:tasks                                                      │
└──────────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ SENTINELLE (Security Guardian - Consumer)                                    │
│ ├─ Consomme: relais:tasks                                                    │
│ ├─ Valide: ACL (users.yaml)                                                  │
│ ├─ Filtre: contenu (guardrails pre/post LLM)                                │
│ └─ Publie: relais:tasks (ou refuse)                                         │
└──────────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ ATELIER (Workshop - Transformer)                                             │
│ ├─ Consomme: relais:tasks                                                    │
│ ├─ Charge: SOUL (personality), prompts, contexte long-term                  │
│ ├─ Appelle: LiteLLM (avec retry + DLQ sur failure)                          │
│ └─ Publie: relais:messages:outgoing:{channel}                               │
└──────────────────────────────────────────────────────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
          ▼                   ▼                   ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│ SOUVENIR         │ │ AIGUILLEUR       │ │ ARCHIVISTE       │
│ (Memory Store)   │ │ (Outbound Relay) │ │ (Observer/Logger)│
│                  │ │                  │ │                  │
│ ├─ Short-term:   │ │ ├─ Discord       │ │ ├─ JSONL logs    │
│ │  Redis List    │ │ ├─ Telegram      │ │ ├─ SQLite audit  │
│ │  (20 msgs)     │ │ ├─ Slack         │ │ └─ Retention     │
│ │                │ │ ├─ REST API      │ │    (90d/1y/∞)   │
│ └─ Long-term:    │ │ └─ [extensible]  │ │                  │
│    SQLite        │ │                  │ │ Logs all streams │
│    (persistent)  │ │ → External APIs  │ │                  │
└──────────────────┘ └──────────────────┘ └──────────────────┘
          │                   │                   │
          └───────────────────┼───────────────────┘
                              │
                              ▼
                    [External Users]
```

---

## Inventaire des Briques

### Phase 1-3 (MVP Core Loop) ✅

| Brique | Type | Rôle | Taxonomie |
|--------|------|------|-----------|
| **Portail** | consumer | Valide messages entrants, applique reply_policy | Consumer |
| **Sentinelle** | consumer | Vérifie ACL, filtre contenu | Consumer |
| **Atelier** | transformer | Assemble SOUL, appelle LLM, gère retries + DLQ | Transformer |
| **Souvenir** | consumer | Stocke contexte court-terme (Redis) et long-terme (SQLite) | Consumer |
| **Aiguilleur** | producer | Relaye réponses vers Discord/Telegram/Slack/REST | Producer |
| **Archiviste** | observer | Enregistre logs JSONL/SQLite, gère rétention | Observer |

### Phase 4 (À venir)

| Brique | Type | Rôle |
|--------|------|------|
| **Crieur** | transformer | Push notifications proactives multi-canal |
| **Veilleur** | producer | Planification CRON (APScheduler) + backup |
| **Guichet** | transformer | Webhooks entrants HMAC-signés |
| **Forgeron** | batch | Génération skills automatiques (1×/jour) |

### Phase 6 (Admin & Monitoring)

| Brique | Type | Rôle |
|--------|------|------|
| **Vigile** | admin | Hot reload, commandes NLP, contrôle supervisord |
| **Tisserand** | interceptor | Middleware chain pre/post LLM |
| **Tableau** | admin + relay | Interface TUI (Textual) bidirectionnelle |
| **Scrutateur** | observer | Métriques Prometheus/Loki |

---

## Démarrage rapide

### Prérequis

- Python ≥ 3.11
- Redis (≥ 5.0)
- uv ou pip
- supervisord (optionnel, pour orchestration multi-process)

### Installation

```bash
# 1. Cloner le projet
git clone <repo-url>
cd relais

# 2. Créer l'environnement
python -m venv venv
source venv/bin/activate  # ou `venv\Scripts\activate` sur Windows

# 3. Installer les dépendances
uv pip install -e .
# ou avec pip
pip install -e .

# 4. Initialiser les répertoires locaux (~/.relais/)
# Cette commande crée la structure complète et copie les fichiers par défaut
# y compris les templates de prompts (whatsapp, telegram, out_of_hours, etc.)
python -c "from common.init import initialize_user_dir; from pathlib import Path; initialize_user_dir(Path('.'))"

# 5. Appliquer les migrations Souvenir (SQLite)
alembic upgrade head

# 6. Configurer .env
cp .env.example .env
# Éditez .env avec vos clés API (OPENROUTER_API_KEY, DISCORD_BOT_TOKEN, etc.)
```

### Lancer le système (développement)

**Option A : Avec supervisord (recommandé)**

```bash
supervisord -c supervisord.conf
supervisorctl status  # Vérifier l'état
supervisorctl logs portail  # Voir les logs
supervisorctl restart atelier  # Redémarrer une brique
```

**Option B : Manuellement (3 terminaux)**

```bash
# Terminal 1 - Redis
redis-server config/redis.conf

# Terminal 2 - LiteLLM proxy
uv run --with "litellm[proxy]" --with backoff \
  litellm --config config/litellm.yaml --port 4000

# Terminal 3 - Briques (dans l'ordre)
uv run python portail/main.py     # Terminal 3a
uv run python sentinelle/main.py  # Terminal 3b
uv run python atelier/main.py     # Terminal 3c
uv run python souvenir/main.py    # Terminal 3d
uv run python aiguilleur/discord/main.py  # Terminal 3e
uv run python archiviste/main.py  # Terminal 3f
```

### Vérifier le pipeline

```bash
# Envoyer un message test via l'API REST (une fois Aiguilleur REST implémenté)
curl -X POST http://localhost:8000/message \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_123",
    "channel": "rest",
    "text": "Hello RELAIS"
  }'

# Ou simplement envoyer un message Discord/Telegram mentionnant le bot
```

---

## Structure de configuration

### ~/.relais/ (répertoire utilisateur)

Créé automatiquement au premier lancement via `initialize_user_dir()`, qui copie tous les fichiers par défaut.

```
~/.relais/
├── config/
│   ├── config.yaml              (copié de config/config.yaml.default)
│   ├── profiles.yaml            (profils LLM)
│   ├── users.yaml               (ACL utilisateurs)
│   ├── reply_policy.yaml        (politique réponse auto)
│   ├── mcp_servers.yaml         (serveurs MCP globaux)
│   └── HEARTBEAT.md             (tâches CRON planifiées)
├── soul/
│   ├── SOUL.md                  (personnalité principale)
│   └── variants/
│       ├── SOUL_concise.md
│       └── SOUL_professional.md
├── prompts/                     (templates de prompts copiés au premier lancement)
│   ├── whatsapp_default.md
│   ├── telegram_default.md
│   ├── out_of_hours.md
│   ├── vacation.md
│   └── in_meeting.md
├── storage/
│   ├── memory.db                (SQLite long-term context, géré par Alembic)
│   ├── audit.db                 (SQLite audit trail)
│   └── archive/                 (JSONL logs)
├── logs/
├── redis.sock                   (Socket Unix Redis)
├── supervisor.sock              (Socket supervisord)
└── supervisord.pid
```

### Résolution de configuration (cascade)

`~/.relais/config/` (utilisateur) → `/opt/relais/config/` (système) → `./config/` (projet)

Premier fichier trouvé gagne. La variable d'environnement `RELAIS_HOME` surcharge `~/.relais` pour Docker, CI ou multi-instance.

Toutes les briques utilisent `resolve_config_path("fichier.yaml")` depuis `common/config_loader.py`.
Le stockage persistant (SQLite) utilise `resolve_storage_dir()` → `~/.relais/storage/`.

---

## Variables d'environnement (.env)

| Variable | Description | Exemple |
|----------|-------------|---------|
| `OPENROUTER_API_KEY` | Clé API OpenRouter | `sk-or-xxx` |
| `REDIS_SOCKET_PATH` | Path socket Unix Redis | `./.relais/redis.sock` |
| `REDIS_PASSWORD` | Mot de passe Redis admin | `xxx` |
| `REDIS_PASS_PORTAIL` | MDP brique Portail | `xxx` |
| `REDIS_PASS_SENTINELLE` | MDP brique Sentinelle | `xxx` |
| `REDIS_PASS_ATELIER` | MDP brique Atelier | `xxx` |
| `REDIS_PASS_SOUVENIR` | MDP brique Souvenir | `xxx` |
| `DISCORD_BOT_TOKEN` | Token Discord bot | `xxx` |
| `TELEGRAM_BOT_TOKEN` | Token Telegram bot | `xxx` |
| `SLACK_BOT_TOKEN` | Token Slack app | `xoxb-xxx` |
| `SLACK_SIGNING_SECRET` | Secret signature Slack | `xxx` |
| `LITELLM_BASE_URL` | URL proxy LiteLLM | `http://localhost:4000/v1` |
| `LITELLM_MASTER_KEY` | Master key LiteLLM | `sk-changeme` |
| `LITELLM_MODEL` | Modèle LLM par défaut | `mistral-small-2603` |
| `RELAIS_HOME` | Chemin alternatif ~/.relais | Optionnel |

---

## Flux de données (Redis Streams)

### Noms de streams

```
relais:messages:incoming:{channel}   ← Messages entrants (Discord, Telegram, REST)
relais:tasks                         ← Tâches validées (Portail → Sentinelle → Atelier)
relais:messages:outgoing:{channel}   ← Messages sortants (Atelier → Aiguilleur)
relais:tasks:failed                  ← Dead Letter Queue (Atelier sur retry épuisés)
relais:context:{user_id}             ← Historique utilisateur court-terme (Souvenir)
relais:events:{brick_name}           ← Événements briques (Pub/Sub)
relais:notifications:{role}          ← Notifications à diffuser (Crieur)
relais:push:{urgency}                ← Pushes proactifs (Crieur)
```

### Garanties

- **At-least-once delivery** : Chaque message dans un stream est livré ≥ 1 fois
- **Consumer groups** : Plusieurs instances d'une brique = déduplication automatique
- **PEL (Pending Entry List)** : Tâches non-ACK reviennent à la file si crash

---

## Modules common/ (Phase 1) ✅

| Module | Responsabilité |
|--------|-----------------|
| `config_loader.py` | Cascade config (~/.relais/ → /opt/ → ./) |
| `envelope.py` | Structure message standardisée |
| `redis_client.py` | Factory AsyncRedis avec ACL |
| `init.py` | initialize_user_dir() |
| `shutdown.py` | GracefulShutdown (SIGTERM/SIGINT) |
| `stream_client.py` | StreamConsumer / StreamProducer |
| `event_publisher.py` | EventPublisher (Pub/Sub) |
| `health.py` | health() standard |
| `markdown_converter.py` | MD → Telegram/Slack/plaintext |

---

## Briques implémentées

### Portail (`portail/`)

Gatekeeper entrant. Valide le format, applique reply_policy (DND, vacation, in_meeting), filtre par canal.

**Fichiers clés:**
- `portail/main.py` — Consumer group "portail"
- `portail/reply_policy.py` — Chargement reply_policy.yaml
- `portail/prompt_loader.py` — Chargement prompts utilisateur

**Streams:**
- Consomme: `relais:messages:incoming:*`
- Publie: `relais:tasks`

---

### Sentinelle (`sentinelle/`)

Gardienne de sécurité. Vérifie ACL (users.yaml), applique guardrails (filtres pre/post-LLM).

**Fichiers clés:**
- `sentinelle/main.py` — Consumer group "sentinelle"
- `sentinelle/acl.py` — ACLManager (users.yaml)
- `sentinelle/guardrails.py` — ContentFilter

**Streams:**
- Consomme: `relais:tasks`
- Publie: `relais:tasks` (ou refuse si ACL ko)

---

### Atelier (`atelier/`)

Cerveau du système. Assemble SOUL (personnalité), charge contexte long-term, appelle LiteLLM avec retry + DLQ.

**Fichiers clés:**
- `atelier/main.py` — Consumer group "atelier"
- `atelier/executor.py` — execute_with_resilience() (retry backoff + DLQ)

**Résilience:**
- ✅ Retry 3× sur ConnectError/TimeoutException (délais: 2s, 5s, 15s)
- ✅ Fallback Ollama si LiteLLM down (optionnel)
- ✅ Dead Letter Queue `relais:tasks:failed` si retries épuisés
- ✅ XACK conditionnel (jamais sur erreur transiente)

**Streams:**
- Consomme: `relais:tasks`
- Publie: `relais:messages:outgoing:{channel}` (succès)
- Publie: `relais:tasks:failed` (échec après retries)

---

### Souvenir (`souvenir/`)

Mémoire hybride du système.

**Fichiers clés:**
- `souvenir/main.py` — Consumer group "souvenir"
- `souvenir/context_store.py` — Redis List (court-terme, 20 msgs, TTL 24h)
- `souvenir/long_term_store.py` — SQLite (persistent)

**Stockage:**
- Short-term: Redis List `relais:context:{user_id}` (20 dernier messages, TTL 24h)
- Long-term: SQLite `~/.relais/storage/messages.db` (illimité, queryable)

**Streams:**
- Consomme: `relais:messages:outgoing:*` (aussi réponses)
- Écrit: Redis List + SQLite

---

### Aiguilleur (`aiguilleur/`)

Relayeur vers canaux externes. Architecture base abstraite (AiguilleurBase) + implémentations par canal.

**Fichiers clés:**
- `aiguilleur/base.py` — AiguilleurBase ABC
- `aiguilleur/discord/main.py` — Discord relay (discord.py)
- `aiguilleur/telegram/main.py` — (Phase 5)
- `aiguilleur/slack/main.py` — (Phase 5)
- `aiguilleur/rest/main.py` — REST API relay (Phase 5)

**Contrat AiguilleurBase:**
```python
async def receive() -> Envelope  # Reçoit message entrant
async def send(envelope) -> str  # Envoie message sortant
def format_for_channel(text) -> str  # Formate texte (MD→Telegram etc)
```

**Streams:**
- Consomme: `relais:messages:outgoing:{channel}` (sortant)
- Publie: `relais:messages:incoming:{channel}` (entrant si applicable)

---

### Archiviste (`archiviste/`)

Observer pur. Enregistre tous les messages (JSONL + SQLite audit), gère rétention.

**Fichiers clés:**
- `archiviste/main.py` — Consumer group "archiviste"
- `archiviste/cleanup_retention.py` — CleanupManager (retention policy)

**Rétention:**
- JSONL: 90 jours
- SQLite: 1 an
- Audit: illimité

**Stockage:**
- `~/.relais/storage/archive/*.jsonl` (JSONL logs par date)
- `~/.relais/storage/audit.db` (SQLite audit complet)

**Streams:**
- Consomme: tous les streams (observe tout)
- Écrit: JSONL + SQLite

---

## Tests

### Couverture cible

80% minimum (règle projet).

### Lancer les tests

```bash
pytest tests/ -v --cov=common,portail,sentinelle,atelier,souvenir,aiguilleur,archiviste --cov-report=term-missing
```

### Types de tests

- **Unit** : Chaque module isolé (+ Redis mock)
- **Integration** : Pipeline complet (+ Redis réel)
- **E2E** : Message Discord entrant → Réponse Discord (test client discord.py)

---

## Contribuer

Voir [CONTRIBUTING.md](docs/CONTRIBUTING.md) pour:
- Setup dev (uv, Redis, supervisord)
- Architecture des tests
- Checklist ajout nouvelle brique
- Contrat aiguilleur

---

## Documentation supplémentaire

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — Taxonomie briques, flow diagrammes, dependency map
- **[docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)** — Dev setup, tests, checklist nouveaux bricks
- **[plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md](plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md)** — Spécification complète
- **[.claude/plan/relais-implementation.md](.claude/plan/relais-implementation.md)** — Plan d'implémentation phases (état de progression)

---

## Licences

RELAIS — Licence MIT

Dépendances principales:
- redis-py 5.0+
- litellm 1.25+
- httpx 0.27+
- pydantic 2.9+
- aiosqlite 0.20+

Voir `pyproject.toml` pour la liste complète.

---

**Dernière mise à jour:** 2026-03-28

État: MVP Phase 1-3 ✅ opérationnel
Prochaines phases: Phase 4 (Crieur, Veilleur, Guichet, Forgeron)
