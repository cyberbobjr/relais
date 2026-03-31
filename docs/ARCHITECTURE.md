# RELAIS — Architecture Technique

**Dernière mise à jour:** 2026-03-30
**Phases implémentées:** 1, 2, 3 (MVP core loop), 5 (Outils internes + MCP stdio/SSE), 5c (déduplication streaming Discord)

---

## Table des matières

1. [Taxonomie des briques](#taxonomie-des-briques)
2. [Flux de données (Redis Streams)](#flux-de-données-redis-streams)
3. [Inventaire des streams](#inventaire-des-streams)
4. [Ordre d'initialisation](#ordre-dinitialisation)
5. [Garanties de livraison](#garanties-de-livraison)
6. [Résolution de configuration](#résolution-de-configuration)
7. [Carte de dépendances common/](#carte-de-dépendances-common)

---

## Taxonomie des briques

Les briques RELAIS sont classées en quatre catégories selon leur rôle dans le pipeline:

### Consumer (Consommateur)

**Rôle:** Lit depuis un stream, applique logique, publie ou rejette.

**Pattern:**
```python
async def main():
    consumer = StreamConsumer(redis_conn, stream="relais:tasks", group="portail")
    async for message in consumer.consume():
        result = process(message)
        await consumer.ack(message["id"])
```

**Exemples:** Portail, Sentinelle, Souvenir

**Propriétés:**
- Consommation garantie (consumer groups Redis)
- XACK après traitement (at-least-once)
- Multiple instances = déduplication automatique

---

### Producer (Producteur)

**Rôle:** Génère des messages, les publie dans un stream, puis terminer.

**Pattern:**
```python
async def main():
    producer = StreamProducer(redis_conn)
    await producer.publish("relais:tasks", {
        "user_id": "...",
        "text": "...",
    })
```

**Exemples:** Veilleur (tâches CRON), Guichet (webhooks)

**Propriétés:**
- Pas de consumer group
- Fire-and-forget
- Simplifié pour "injecteurs" externes

---

### Transformer (Transformateur)

**Rôle:** Lit, applique logique complexe, peut écrire à plusieurs streams, peut rejeter.

**Pattern:**
```python
async def main():
    consumer = StreamConsumer(redis_conn, stream="relais:tasks", group="atelier")
    async for message in consumer.consume():
        try:
            result = await execute_with_resilience(message)
            await producer.publish("relais:messages:outgoing:discord", result)
        except ExhaustedRetriesError:
            await producer.publish("relais:tasks:failed", {"error": ..., "payload": message})
        finally:
            await consumer.ack(message["id"])
```

**Exemples:** Atelier, Crieur

**Propriétés:**
- Consomme + produit
- Logique complexe autorisée
- Rejection via streams alternatifs (DLQ)

---

### Observer (Observateur)

**Rôle:** Lit tous les streams, enregistre, alerte, mais n'interfère jamais avec le pipeline.

**Pattern:**
```python
async def main():
    consumer = StreamConsumer(redis_conn, stream="relais:messages:outgoing:discord", group="archiviste")
    async for message in consumer.consume():
        await archive(message)  # JSONL + SQLite
        await consumer.ack(message["id"])
```

**Exemples:** Archiviste, Scrutateur

**Propriétés:**
- Lecture seule (jamais de rejection)
- Peut consommer plusieurs streams
- Idéal pour logging/monitoring

---

## Flux de données (Redis Streams)

```
ENTRANT (Canaux externes)
├─ Discord mentions/DMs → Aiguilleur/discord (producer)
│                         └─ relais:messages:incoming:discord
├─ Telegram messages    → Aiguilleur/telegram (phase 5)
│                         └─ relais:messages:incoming:telegram
├─ Slack events        → Aiguilleur/slack (phase 5)
│                         └─ relais:messages:incoming:slack
└─ REST POST webhooks  → Guichet (phase 4)
                         └─ relais:messages:incoming:rest

PIPELINE CORE
├─ relais:messages:incoming:* (input)
│   ▼
│ PORTAIL (consumer)
│ ├─ Valide format (Envelope)
│ ├─ Applique reply_policy
│ └─ Publie si accepté
│   ▼
│ relais:tasks (intermediate)
│   ▼
│ SENTINELLE (consumer)
│ ├─ Vérifie ACL (users.yaml)
│ ├─ Applique guardrails pré-LLM
│ └─ Publie si accepté
│   ▼
│ relais:tasks (validated)
│   ▼
│ ATELIER (transformer)
│ ├─ Charge SOUL + contexte long-term
│ ├─ Exécute boucle agentique via AgentExecutor (deepagents.create_deep_agent)
│ ├─ Streams output token-by-token → relais:messages:streaming:{channel}:{correlation_id}
│ ├─ Publie réponse si succès
│ └─ Publie en DLQ si échec après retries
│   ├─ relais:messages:outgoing:*
│   ├─ relais:messages:streaming:{channel}:{correlation_id}
│   └─ relais:tasks:failed (DLQ)
│       ▼
│ SOUVENIR (consumer)
│ ├─ Historique court-terme (Redis List)
│ └─ Historique long-terme (SQLite)
│
└─ ARCHIVISTE (observer)
   └─ Observe tous les streams
      └─ Archive JSONL + SQLite audit

SORTANT (Canaux externes)
└─ relais:messages:outgoing:*
   ▼
   AIGUILLEUR/{channel} (producer)
   ├─ Discord → Discord API  (avec déduplication streaming — voir ci-dessous)
   ├─ Telegram → Telegram API
   ├─ Slack → Slack API
   └─ REST → HTTP webhook
```

### Déduplication streaming Discord

Pour les canaux dont `streaming: true` dans `channels.yaml`, Atelier publie à la fois :
- les chunks progressifs sur `relais:messages:streaming:{channel}:{correlation_id}` (rendu live)
- l'enveloppe finale sur `relais:messages:outgoing:{channel}` (confirmé)

> **Note :** Atelier lit `channels.yaml` **une seule fois au démarrage** pour construire la liste des canaux
> streaming-capable. Tout changement du champ `streaming:` dans ce fichier nécessite un redémarrage
> d'Atelier (`supervisorctl restart atelier`) pour être pris en compte — en plus du redémarrage d'Aiguilleur.

Sans mécanisme de déduplication, Aiguilleur Discord enverrait deux messages identiques. Le mécanisme Option C résout ce problème :

```
Atelier publie Pub/Sub → relais:streaming:start:discord
    │
    ↓
Aiguilleur: _handle_streaming_message() spawné
    ├── channel.send("▌")                      → discord_msg_id obtenu
    └── SETEX relais:streamed_msg:{corr_id} 300 {discord_msg_id}
        (chunks progressifs → msg.edit())

Atelier XADD → relais:messages:outgoing:discord (metadata.streamed=True)
    │
    ↓
Aiguilleur: consume_outgoing_stream()
    ├── metadata["streamed"] == True ?
    │   ├── OUI → GET relais:streamed_msg:{corr_id}
    │   │         ├── clé présente → partial.edit(content) + DELETE clé  ✓
    │   │         └── clé absente (TTL expiré) → channel.send() (fallback)
    └── NON → channel.send() (comportement normal)
```

**Clé Redis impliquée** : `relais:streamed_msg:{correlation_id}` — String, TTL 300s, valeur = Discord message ID.

---

## Inventaire des streams

### Streams d'entrée

| Stream | Source | Producteur | Contenu |
|--------|--------|-----------|---------|
| `relais:messages:incoming:discord` | Discord API | Aiguilleur/discord | Enveloppe message |
| `relais:messages:incoming:telegram` | Telegram API | Aiguilleur/telegram | Enveloppe message |
| `relais:messages:incoming:slack` | Slack API | Aiguilleur/slack | Enveloppe message |
| `relais:messages:incoming:rest` | HTTP POST | Guichet | Enveloppe message |

### Streams intermédiaires

| Stream | Consumer | Producteur | Contenu |
|--------|----------|-----------|---------|
| `relais:tasks` | Sentinelle/Atelier | Portail | Tâche validée |
| `relais:context:{session_id}` | (Redis List) | Souvenir | Historique court-terme (max 20 msgs, TTL 24h) |
| `relais:memory:request` | Souvenir | Atelier | Requêtes mémoire (`get`) avant exécution agentique |
| `relais:memory:response` | Atelier | Souvenir | Réponses mémoire (contexte court-terme) |
| `relais:messages:streaming:{channel}:{correlation_id}` | Aiguilleur/{channel} | Atelier | Chunks streaming progressif (Discord edit mode) |
| `relais:streamed_msg:{correlation_id}` | (Redis String, TTL 300s) | Aiguilleur/discord | Discord message ID du placeholder streaming (déduplication) |

### Streams de sortie

| Stream | Producteur | Consommateur | Contenu |
|--------|-----------|--------------|---------|
| `relais:messages:outgoing:discord` | Atelier | Aiguilleur/discord | Message formaté Discord |
| `relais:messages:outgoing:telegram` | Atelier | Aiguilleur/telegram | Message formaté Telegram |
| `relais:messages:outgoing:slack` | Atelier | Aiguilleur/slack | Message formaté Slack |
| `relais:messages:outgoing:rest` | Atelier | Aiguilleur/rest | JSON payload |

### Streams d'erreur et monitoring

| Stream | Producteur | Consommateur | Contenu |
|--------|-----------|--------------|---------|
| `relais:tasks:failed` | Atelier | Archiviste/Veilleur | Message + raison d'erreur |
| `relais:events:{brick}` | Tout brick | Scrutateur/Vigile | Événement (Pub/Sub) |
| `relais:notifications:{role}` | Crieur | Aiguilleur cibles | Notification urgente |
| `relais:push:{urgency}` | Crieur | Aiguilleur cibles | Push proactif |

---

## Ordre d'initialisation

### Phase 0 — Initialization utilisateur (synchrone, par brick)

**Chaque brick exécute `initialize_user_dir()` au démarrage:**
- Crée `~/.relais/` si absent
- Copie fichiers de config par défaut (idempotent)
- Lance avant `asyncio.run()` — garantit structure avant async

Aucune coordination requise: appels concurrents sont sûrs (idempotent, basé sur existence de fichiers).

### Supervisord priorities (après ~/. relais/ prêt)

```
Priority 1 (infra basique)
  └─ courier (Redis)

Priority 8 (observers — pas de dépendance)
  └─ archiviste

Priority 10 (core pipeline)
  ├─ portail
  ├─ sentinelle
  ├─ atelier
  ├─ souvenir
  └─ (future: crieur, veilleur)

Priority 20 (relays — dépend de core)
  └─ aiguilleur-discord
     (+ phase 5: telegram, slack, rest)

Priority 30 (admin optionnel — autostart=false)
  └─ tableau (TUI)
```

**Rationale:**
1. initialize_user_dir() se lance dans chaque __main__, avant Redis (asynchrone)
2. Redis doit être disponible avant tout (async)
3. Observers peuvent démarrer indépendamment (pas de dépendance ordonnée)
4. Core pipeline en ordre logique (Portail → Sentinelle → Atelier → Souvenir)
5. Relays attendent que core soit prêt
6. Admin/TUI optionnel, démarrage manuel

---

## Garanties de livraison

### At-least-once (par défaut)

Chaque message est livré ≥ 1 fois:

```
1. Message écrit → Redis stream
2. Consumer XREAD (lis)
3. Logique
4. XACK (confirme)   ← Si erreur avant XACK, message reste dans PEL
5. Message supprimé du PEL
```

**Implications:**
- Pas de perte de message (crash safe)
- Possible duplication si process crash entre logique et XACK
- Idempotence recommandée dans logique métier

### Retry avec backoff (Atelier)

Atelier utilise la configuration de résilience définie dans `profiles.yaml` (champs `retry_attempts`, `retry_delays`). Sur erreur transiente, l'exécution reste en PEL pour re-livraison automatique. Les erreurs non-retriable (`AgentExecutionError`) sont routées vers la DLQ.

**Délais typiques:** 2s → 5s → 15s (total max 22s, configurable par profil)

**Comportement:**
- Erreur transiente (connexion, timeout) → pas d'ACK → reste en PEL pour re-livraison
- `AgentExecutionError` → route vers DLQ `relais:tasks:failed` + ACK
- Message en DLQ = **XACK appliqué** (pas perdu, mais marqué comme failed)

### Dead Letter Queue (DLQ)

`relais:tasks:failed` stream de fallback:

```json
{
  "payload": "{envelope_json}",
  "reason": "AgentExecutionError: agent failed after 3 retries",
  "attempts": 3,
  "failed_at": "2026-03-27T14:23:45.123Z"
}
```

**Consommateurs:**
- **Archiviste** : Enregistre pour audit + alertes ERROR
- **Veilleur** (phase 4) : Peut rejouer manuellement (CLI ou Vigile)
- **Scrutateur** (phase 6) : Alerte Prometheus/Loki

---

## Résolution de configuration

### Cascade de fichiers

Chaque brick charge config.yaml en ce ordre:

1. `~/.relais/config/config.yaml` (utilisateur)
2. `/opt/relais/config/config.yaml` (système)
3. `./config/config.yaml` (projet)

**Premier trouvé gagne.**

### Chargement par spécialité

| Brick | Config fichiers |
|-------|-----------------|
| Portail | `config.yaml`, `reply_policy.yaml`, `prompts/*.md` |
| Sentinelle | `config.yaml`, `users.yaml`, `guardrails.yaml` |
| Atelier | `config.yaml`, `profiles.yaml`, `soul/SOUL.md`, `mcp_servers.yaml` |
| Souvenir | `config.yaml`, `~/.relais/storage/memory.db` (SQLite via Alembic) |
| Archiviste | `config.yaml` (retention policy) |
| Aiguilleur | `config.yaml`, `aiguilleur/{canal}.yaml` |

### Fonctions `common/config_loader.py`

```python
from common.config_loader import get_relais_home, resolve_config_path, resolve_storage_dir

# Répertoire utilisateur (respect de RELAIS_HOME env var)
home = get_relais_home()            # ~/.relais  (ou $RELAIS_HOME si défini)

# Résolution de fichier de config avec cascade
path = resolve_config_path("users.yaml")   # cascade: ~/.relais/config/ → /opt/relais/config/ → ./config/

# Répertoire de stockage persistant (SQLite, etc.)
storage = resolve_storage_dir()     # ~/.relais/storage/  (créé si absent)
```

Toutes les briques utilisent `resolve_config_path()` et `resolve_storage_dir()` — **jamais** `Path.home() / ".relais"` directement.
La variable d'environnement `RELAIS_HOME` permet de dérouter vers un autre chemin (Docker, multi-instance, CI).

### Initialisation des répertoires utilisateur

**Au premier lancement de TOUT brick** (Portail, Sentinelle, Atelier, Souvenir, Archiviste, Aiguilleur/Discord, etc.), la structure `~/.relais/` est **auto-créée et pré-peuplée** — aucune configuration manuelle requise.

Chaque brick appelle `initialize_user_dir()` **synchroniquement** dans son `__main__` block, **avant** `asyncio.run()`:

```python
# Dans portail/main.py, atelier/main.py, etc.
from common.init import initialize_user_dir
from pathlib import Path
import asyncio

if __name__ == "__main__":
    initialize_user_dir(Path(__file__).parent.parent)  # Chemin du projet
    asyncio.run(main())
```

**Fichiers créés automatiquement:**
- `config/*.yaml` (config, profiles, users, reply_policy, mcp_servers, HEARTBEAT)
- `soul/SOUL.md` et variants (`SOUL_concise.md`, `SOUL_professional.md`)
- **Prompt templates** (`whatsapp_default.md`, `telegram_default.md`, `out_of_hours.md`, `in_meeting.md`, `vacation.md`)
- Répertoires de stockage: `logs/`, `storage/`, `backup/`, `media/`, `skills/`

**Propriétés critiques:**
- **Idempotente** — Safe à appeler depuis plusieurs bricks concurrents. Les fichiers existants ne sont JAMAIS écrasés.
- **Synchrone** — Bloque un instant (~10ms), nécessaire pour garantir la structure avant que les bricks commencent.
- **Sans dépendance à Redis** — S'exécute avant connexion Redis, donc ne peut pas échouer silencieusement.

Pas de setup manuel: lancer n'importe quel brick initialise automatiquement l'environnement utilisateur.

---

## Carte de dépendances common/

```
common/
├── config_loader.py
│   └── (dépends: pathlib, yaml, os)
│
├── envelope.py
│   └── (dépends: pydantic)
│
├── redis_client.py
│   ├── (dépends: redis.asyncio)
│   └── (uses: config_loader)
│
├── init.py
│   ├── (dépends: pathlib, shutil)
│   └── (uses: config_loader)
│
├── shutdown.py
│   ├── (dépends: asyncio, signal)
│   └── (uses: logging)
│
├── stream_client.py
│   ├── (dépends: redis.asyncio, asyncio)
│   └── (uses: redis_client, envelope)
│
├── event_publisher.py
│   ├── (dépends: redis.asyncio)
│   └── (uses: redis_client)
│
├── health.py
│   ├── (dépends: asyncio, psutil [opt])
│   └── (uses: redis_client)
│
└── markdown_converter.py
    └── (dépends: re, html2text [opt])

Exports (utilisés par briques)
├── StreamConsumer
├── StreamProducer
├── EventPublisher
├── health()
├── Envelope, PushEnvelope, MediaRef
├── AsyncRedis, create_redis_conn()
├── GracefulShutdown
├── initialize_user_dir()
├── ConfigLoader
├── convert_md_to_telegram()
├── convert_md_to_slack_mrkdwn()
├── strip_markdown()
└── markdown_to_html()
```

### Ordre de chargement recommandé

1. `config_loader` (basique, pas de dépendance)
2. `envelope` (structures)
3. `redis_client` (factory)
4. `stream_client` (dépends redis_client + envelope)
5. `event_publisher` (dépends redis_client)
6. `health`, `shutdown`, `markdown_converter` (independantes)
7. `init` (optionnel, au démarrage)

---

## Contrat Aiguilleur (Relays)

Tous les relays héritent d'`AiguilleurBase` (abc):

```python
from aiguilleur.base import AiguilleurBase
from common.envelope import Envelope

class AiguilleurDiscord(AiguilleurBase):
    async def receive(self) -> Envelope:
        """Reçoit message du canal externe, retourne Envelope."""
        # Discord webhooks entrants
        # Construit Envelope avec user_id, channel, text, media

    async def send(self, envelope: Envelope) -> str:
        """Envoie message au canal externe."""
        # Appelle Discord API
        # Retourne message_id si succès

    def format_for_channel(self, text: str) -> str:
        """Formate texte pour canal (MD→Discord)."""
        # Discord supporte MD de base + ** gras ** etc
        # Retourne texte formaté
```

### Implémentations actuelles

- **Aiguilleur/Discord** ✅ — discord.py bot
  - Handles mentions + DMs
  - format_for_channel() → Discord markdown

### Implémentations Phase 5

- **Aiguilleur/Telegram** — python-telegram-bot
  - format_for_channel() → MarkdownV2

- **Aiguilleur/Slack** — slack-bolt
  - format_for_channel() → mrkdwn

- **Aiguilleur/REST** — FastAPI
  - receive() ← HTTP POST /message
  - send() → HTTP POST callback

---

## Événements système (Pub/Sub)

Briques peuvent émettre événements via `EventPublisher`:

```python
from common.event_publisher import EventPublisher

publisher = EventPublisher(redis_conn)

# Atelier — task completed
await publisher.emit("atelier:task_completed", {
    "task_id": "...",
    "duration_ms": 123,
    "model": "mistral-small",
})

# Sentinelle — ACL denied
await publisher.emit("sentinelle:acl_denied", {
    "user_id": "...",
    "reason": "not_in_allowed_users",
})

# Archiviste — archive rotation
await publisher.emit("archiviste:archive_rotated", {
    "filename": "archive_2026_03_27.jsonl",
    "message_count": 4567,
})
```

**Souscripteurs (Pub/Sub):**
- **Scrutateur** (phase 6) — expose /metrics (Prometheus)
- **Vigile** (phase 6) — alerte sur seuils critiques

---

## Performance & Scalabilité

### Limites actuelles

- **Single Redis instance** — OK pour <1000 msg/jour
- **Single Atelier instance** — ~50 msg/min (limité par LLM latency)
- **Single Souvenir instance** — illimité (asyncio)

### Scaling horizontal

**Consumer groups permettent:**

```bash
# Démarrer N instances Atelier
for i in {1..3}; do
  INSTANCE_ID=$i uv run python atelier/main.py &
done
```

Redis déduire automatiquement = load balancing gratuit.

**Limites:**
- Pas de sharding Redis (une seule instance)
- DLQ + Dead Letter Handler bottleneck (une seule Archiviste)
- Souvenir SQLite mono-thread (lock contention possible)

---

## Sécurité

### Redis ACL

Chaque brick a mot de passe séparé (`.env`). Atelier a accès aux streams de streaming:

```yaml
# redis.conf
user portail +@all ~* >$REDIS_PASS_PORTAIL
user sentinelle +@all ~* >$REDIS_PASS_SENTINELLE
user atelier +@all ~relais:messages:streaming:* >$REDIS_PASS_ATELIER
user souvenir +@all ~relais:messages:outgoing:* >$REDIS_PASS_SOUVENIR
```

### Sentinelle ACL

Utilisateurs contrôlés via `users.yaml`:

```yaml
users:
  benjamin:
    role: admin
    allowed_channels: [discord, telegram, rest]
  alice:
    role: user
    allowed_channels: [discord]
```

### Guardrails LLM

Sentinelle filtre contenu:

```python
if guardrails.is_dangerous_pre_llm(envelope.text):
    # Rejette avant appel LLM

elif guardrails.is_dangerous_post_llm(response.text):
    # Filtre réponse avant envoi
```

---

## Monitoring & Logging

### Logs par brique

```bash
supervisorctl logs portail       # Logs Portail
supervisorctl logs atelier -f    # Follow logs Atelier
tail -f ~/.relais/logs/atelier.log  # Direct file
```

### Alertes Scrutateur (phase 6)

Prometheus metrics via `relais:events:*`:

```
relais_atelier_task_duration_ms
relais_atelier_task_failures_total
relais_sentinelle_acl_denials_total
relais_archiviste_archive_size_bytes
```

---

## Migration & Backup

### Backup automatique (Veilleur, phase 4)

Tâche CRON dans `HEARTBEAT.md`:

```markdown
## Daily Backup 02:00

- SQLite dump `messages.db` → `backup/messages_YYYY-MM-DD.db`
- Redis BGSAVE → `backup/dump_YYYY-MM-DD.rdb`
- Archive JSONL → gzip `archive_YYYY-MM.tar.gz`
```

### Rétention (Archiviste)

```
JSONL logs:  90 jours (conforme RGPD)
SQLite:      1 an
Audit trail: illimité
```

### Migrations Souvenir (Alembic)

Le schéma de SQLite est géré par Alembic. Le chemin de la base de données est résolu via `resolve_storage_dir()` dans `souvenir/migrations/env.py` et `souvenir/long_term_store.py`, qui respecte l'env var `RELAIS_HOME`:

```bash
# Appliquer toutes les migrations (production)
alembic upgrade head
# (Utilise automatiquement ~/.relais/storage/memory.db ou $RELAIS_HOME/storage/memory.db)

# Overrider le chemin si nécessaire (rare)
RELAIS_DB_PATH=/custom/path/memory.db alembic upgrade head

# Générer une nouvelle migration après modification de souvenir/models.py
alembic revision --autogenerate -m "description"

# Vérifier l'état des migrations
alembic current
```

> En test, `LongTermStore._create_tables()` peut être appelé directement (crée le schéma sans Alembic).

---

## Atelier — Exécution LLM et Streaming Progressif

### Architecture AgentExecutor + McpSessionManager

L'Atelier utilise deux classes complémentaires :

- **`AgentExecutor`** (`atelier/agent_executor.py`) — boucle agentique via `deepagents.create_deep_agent()`, gestion des outils LangChain (`list[BaseTool]`) et streaming token-by-token.
- **`McpSessionManager`** (`atelier/mcp_session_manager.py`) — cycle de vie des serveurs MCP (démarrage, sessions, dispatch avec timeout). Séparé de l'exécuteur pour que la logique MCP soit isolée et testable indépendamment.

```python
class AgentExecutor:
    async def execute(self, envelope, context, stream_callback=None) -> str:
        """Execute the agentic loop for an incoming envelope.

        Args:
            envelope: Incoming message envelope.
            context: Short-term conversation context.
            stream_callback: Optional async callable receiving each token chunk.

        Returns:
            Aggregated reply string.
        """
        mcp_tools = await make_mcp_tools(self._mcp_servers)
        all_tools = self._tools + mcp_tools  # BaseTool list
        agent = create_deep_agent(
            model=self._profile.model,   # provider:model-id format
            tools=all_tools,
            system_prompt=self._soul_prompt,
        )
        full_reply = ""
        async for chunk in agent.astream(
            {"messages": self._build_messages(envelope, context)},
            stream_mode="messages",
        ):
            if stream_callback and chunk.content:
                await stream_callback(chunk.content)
            full_reply += chunk.content or ""
        return full_reply
```

### Outils (LangChain `BaseTool`)

`AgentExecutor` accepte une liste de `BaseTool` (LangChain). Les outils internes et MCP sont unifiés sous ce type :

- **`make_skills_tools(skills_dir)`** (`atelier/skills_tools.py`) — retourne des `BaseTool` pour `list_skills` et `read_skill` (découverte des SKILL.md)
- **`make_mcp_tools(mcp_servers)`** (`atelier/mcp_adapter.py`) — charge les outils MCP via `langchain-mcp-adapters` et retourne des `BaseTool`
- Les outils internes et MCP sont concaténés avant la construction de l'agent ; pas de plafonnement différencié

### Streaming Progressif

Atelier publie les tokens d'une réponse au fur et à mesure via `StreamPublisher` (token-by-token grâce à `agent.astream(stream_mode="messages")`) :

```
relais:messages:streaming:{channel}:{correlation_id}
├─ seq: 1, text: "Bonjour", is_final: 0
├─ seq: 2, text: "Bonjour, c'est", is_final: 0
├─ seq: 3, text: "Bonjour, c'est sympa", is_final: 1
└─ (Aiguilleur/Discord édite le message en temps réel)
```

**Throttling Discord:** 80 caractères minimum entre éditions (rate limit 5 req/5s).

### Serveurs MCP — McpSessionManager

`McpSessionManager` isole toute l'infrastructure MCP. Les serveurs configurés dans `config/mcp_servers.yaml` sont démarrés comme sous-processus (stdio) ou connexions HTTP (SSE). Les outils MCP sont ensuite chargés via `langchain-mcp-adapters` :

```
McpSessionManager (lifecycle)
  ↓
  stdio → subprocess (command, args, env)
  sse   → HTTP connection (url)

make_mcp_tools(mcp_servers)  [atelier/mcp_adapter.py]
  ↓
langchain-mcp-adapters → list[BaseTool]
  ↓
Outils injectés dans create_deep_agent(tools=all_tools)
  ↓
McpSessionManager.call_tool(...)
  → asyncio.wait_for(session.call_tool(...), timeout=mcp_timeout)
  → résultat injecté dans la boucle (erreur retournée en string, pas levée)
```

**Flux d'exécution avec tools:**
```
Atelier → AgentExecutor.execute(envelope, context)
  ↓
make_mcp_tools(mcp_servers) → list[BaseTool] MCP
  ↓
all_tools = internal_tools + mcp_tools
  ↓
create_deep_agent(model, tools=all_tools, system_prompt)
  ↓
agent.astream({"messages": ...}, stream_mode="messages")
  → chunks token-by-token → stream_callback → relais:messages:streaming
  → tool calls gérés automatiquement par DeepAgents
  ↓
Résultat agrégé → relais:messages:outgoing:{channel}
```

### Souvenir — Dual-Stream avec Extracteur Mémoire

Souvenir consomme deux streams:

1. **`relais:memory:request`** (Atelier demande contexte avant exécution agentique)
   - Action `get` → retourne historique court-terme (Redis) ou fallback SQLite
   - Action `append_turn` → ajoute un tour conversation

2. **`relais:messages:outgoing:*`** (observer les réponses finales)
   - `MemoryExtractor` appelle le LLM directement via `langchain.chat_models.init_chat_model` (format `provider:model-id`)
   - Extrait user_facts (faits sur l'utilisateur) avec threshold confiance 0.7
   - Stocke dans SQLite `user_facts` table

**Redis List (`relais:context:{session_id}`):** Cache rapide 20 msgs, TTL 24h
**SQLite (`memory.db`):** Source de vérité, fallback si Redis redémarre

---

## Dépannage

### Message perdu?

1. Vérifier `relais:tasks:failed` (DLQ) — Atelier?
2. Vérifier logs Sentinelle — ACL denial?
3. Vérifier logs Portail — reply_policy rejection?
4. Vérifier Redis PEL: `XINFO STREAM relais:tasks` — messages pending?

### LLM timeout (Atelier)?

1. Vérifier les logs Atelier: `supervisorctl tail atelier -f`
2. Atelier devrait retry 3× avec backoff 2s/5s/15s

### ACL denied?

1. Vérifier `~/.relais/config/users.yaml`
2. Logs Sentinelle: grep "acl_denied"
3. Message en DLQ, chercher raison

---

**Voir aussi:**
- [README.md](../README.md) — Démarrage rapide
- [CONTRIBUTING.md](CONTRIBUTING.md) — Dev workflow
- [plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md](../plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md) — Spécification complète
