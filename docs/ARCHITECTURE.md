# RELAIS — Architecture Technique

**Dernière mise à jour:** 2026-04-01
**Phases implémentées:** 1, 2, 3 (MVP core loop), 5 (Outils internes + MCP stdio/SSE), 5a.6 (multi-provider LLM — base_url/api_key_env), 5a.7 (Discord typing indicator), 5c (déduplication streaming Discord)

---

## Table des matières

1. [Taxonomie des briques](#taxonomie-des-briques)
2. [Flux de données (Redis Streams)](#flux-de-données-redis-streams)
3. [Inventaire des streams](#inventaire-des-streams)
4. [Ordre d'initialisation](#ordre-dinitialisation)
5. [Garanties de livraison](#garanties-de-livraison)
6. [Résolution de configuration](#résolution-de-configuration)
7. [Carte de dépendances common/](#carte-de-dépendances-common)
8. [Le Commandant — Ajouter une commande](#le-commandant--ajouter-une-commande)

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

**Exemples:** Veilleur (tâches CRON), Aiguilleur/rest (webhooks)

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
            await producer.publish("relais:messages:outgoing_pending", result)
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
└─ REST POST webhooks  → Aiguilleur/rest
                         └─ relais:messages:incoming:rest

PIPELINE CORE
├─ relais:messages:incoming:* (input)
│   ▼
│ PORTAIL (consumer)
│ ├─ Valide format (Envelope)
│ ├─ Résout utilisateur (UserRegistry — portail.yaml)
│ ├─ Stamp metadata["user_record"] : dict UserRecord fusionné (rôle + utilisateur)
│ │   (display_name, role, blocked, actions, skills_dirs, allowed_mcp_tools,
│ │    llm_profile, prompt_path)
│ ├─ Applique unknown_user_policy (deny / guest / pending) — config dans portail.yaml
│ │   └─ [pending] publie dans relais:admin:pending_users, puis drop
│ └─ Publie si accepté
│   ▼
│ relais:security (enriched messages)
│   ▼
│ SENTINELLE (consumer — bidirectionnel)
│ ├─ [ENTRANT] Consomme relais:security
│ ├─ [ENTRANT] Vérifie ACL (sentinelle.yaml, lit user_record depuis envelope.metadata)
│ ├─ [ENTRANT] Bifurque :
│ │   ├─ message normal → relais:tasks
│ │   ├─ commande connue + ACL OK → relais:commands
│ │   ├─ commande inconnue → réponse inline "Commande inconnue : /xxx"
│ │   └─ commande non autorisée → réponse inline "Vous n'avez pas la permission..."
│   ▼
│ relais:tasks (security-cleared)
│   ▼
│ ATELIER (transformer)
│ ├─ Charge SOUL + contexte long-term (user_role et prompt_path depuis user_record)
│ ├─ Exécute boucle agentique via AgentExecutor (deepagents.create_deep_agent)
│ ├─ Streams output token-by-token → relais:messages:streaming:{channel}:{correlation_id}
│ ├─ Publie réponse si succès
│ └─ Publie en DLQ si échec après retries
│   ├─ relais:messages:outgoing_pending  (→ Sentinelle outgoing)
│   ├─ relais:messages:streaming:{channel}:{correlation_id}
│   └─ relais:tasks:failed (DLQ)
│       ▼
│ SENTINELLE (consumer — flux sortant)
│ ├─ [SORTANT] Consomme relais:messages:outgoing_pending
│ ├─ [SORTANT] Applique guardrails sortants
│ └─ [SORTANT] Route vers relais:messages:outgoing:{channel}
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
   ├─ Discord → Discord API
   ├─ Telegram → Telegram API
   ├─ Slack → Slack API
   └─ REST → HTTP webhook
```

### Streaming par canal

Pour les canaux dont `streaming: true` dans `channels.yaml`, Atelier publie à la fois :
- les chunks progressifs sur `relais:messages:streaming:{channel}:{correlation_id}` (rendu live)
- l'enveloppe finale sur `relais:messages:outgoing_pending` (→ Sentinelle outgoing → `outgoing:{channel}`)

**Métadonnées du stream `relais:messages:outgoing_pending`:**

Hérités de `relais:security` : `user_record` (dict UserRecord fusionné — source unique d'identité)
Ajoutés par Atelier : `user_message`, `traces`

> **Note :** Atelier lit `channels.yaml` **une seule fois au démarrage** pour construire la liste des canaux
> streaming-capable. Tout changement du champ `streaming:` dans ce fichier nécessite un redémarrage
> d'Atelier (`supervisorctl restart core:atelier`) pour être pris en compte — en plus du redémarrage d'Aiguilleur.

**Discord** (`streaming: false`) —  L'adapter Discord consomme uniquement
`relais:messages:outgoing:discord` et envoie la réponse complète en un seul message.

---

## Inventaire des streams

### Streams d'entrée

| Stream | Source | Producteur | Contenu |
|--------|--------|-----------|---------|
| `relais:messages:incoming:discord` | Discord API | Aiguilleur/discord | Enveloppe message |
| `relais:messages:incoming:telegram` | Telegram API | Aiguilleur/telegram | Enveloppe message |
| `relais:messages:incoming:slack` | Slack API | Aiguilleur/slack | Enveloppe message |
| `relais:messages:incoming:rest` | HTTP POST | Aiguilleur/rest | Enveloppe message |

**Champ `metadata.channel_profile` (stampé par l'Aiguilleur sur tous les streams d'entrée) :**

| Champ | Type | Valeur | Source |
|-------|------|--------|--------|
| `channel_profile` | `string` (optional) | Nom du profil LLM non-résolu | `channels.yaml:profile` → `config.yaml:llm.default_profile` → `"default"` |

Ce champ est résolu par le **Portail** en `llm_profile` et stampé sur `relais:security`.

**Champ `metadata.llm_profile` (stampé par le Portail sur tous les messages en sécurité) :**

| Champ | Type | Valeur | Source |
|-------|------|--------|--------|
| `llm_profile` | `string` | Nom du profil LLM résolu | `channel_profile` (incoming) → `"default"` |

Ce champ est présent sur toutes les enveloppes enrichies. L'Atelier le lit via `envelope.metadata.get("llm_profile", "default")` pour charger le `ProfileConfig` approprié depuis `profiles.yaml`.

### Streams intermédiaires

| Stream | Consumer | Producteur | Contenu |
|--------|----------|-----------|---------|
| `relais:tasks` | Sentinelle/Atelier | Portail | Tâche validée |
| `relais:context:{session_id}` | (Redis List) | Souvenir | Historique court-terme (max 20 msgs, TTL 24h) |
| `relais:memory:request` | Souvenir | Atelier | Requêtes mémoire (`get`) avant exécution agentique |
| `relais:memory:response` | Atelier | Souvenir | Réponses mémoire (contexte court-terme) |
| `relais:messages:streaming:{channel}:{correlation_id}` | Aiguilleur/{channel} | Atelier | Chunks streaming progressif |

### Streams de sortie

| Stream | Producteur | Consommateur | Contenu |
|--------|-----------|--------------|---------|
| `relais:messages:outgoing_pending` | Atelier | Sentinelle (outgoing) | Message tous canaux en attente de validation sortante |
| `relais:messages:outgoing:discord` | Sentinelle | Aiguilleur/discord, Souvenir | Message validé, formaté Discord |
| `relais:messages:outgoing:telegram` | Sentinelle | Aiguilleur/telegram, Souvenir | Message validé, formaté Telegram |
| `relais:messages:outgoing:slack` | Sentinelle | Aiguilleur/slack, Souvenir | Message validé, formaté Slack |
| `relais:messages:outgoing:rest` | Sentinelle | Aiguilleur/rest, Souvenir | Message validé, JSON payload |

### Streams d'erreur et admin

| Stream | Producteur | Consommateur | Contenu |
|--------|-----------|--------------|---------|
| `relais:tasks:failed` | Atelier | Archiviste/Veilleur | Message + raison d'erreur |
| `relais:admin:pending_users` | Portail | Admin (manuel) | Enveloppe d'un utilisateur inconnu en attente de validation (`unknown_user_policy=pending`) |
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
| Portail | `config.yaml`, `portail.yaml` (via `portail.UserRegistry`) |
| Sentinelle | `config.yaml`, `sentinelle.yaml`, `guardrails.yaml` |
| Atelier | `config.yaml`, `profiles.yaml`, `soul/SOUL.md`, `mcp_servers.yaml` |
| Souvenir | `config.yaml`, `~/.relais/storage/memory.db` (SQLite via Alembic) |
| Archiviste | `config.yaml` (retention policy) |
| Aiguilleur | `config.yaml`, `aiguilleur/{canal}.yaml` |

### Résolution du profil LLM

Le profil LLM actif pour un message entrant est résolu dans cet ordre strict :

```
channels.yaml:profile          (stampé par l'Aiguilleur dans envelope.metadata["llm_profile"])
    ↓ absent
config.yaml > llm.default_profile   (fallback système)
    ↓ absent
"default"                      (valeur de repli ultime)
```

**Responsabilité du stamping :** l'**Aiguilleur** lit `ChannelConfig.profile` (champ optionnel dans `channels.yaml`) et stampe `envelope.metadata["llm_profile"]` lors de la création de chaque enveloppe entrante. Si le canal n'a pas de `profile`, l'Aiguilleur utilise `get_default_llm_profile()` de `common/config_loader.py` (lit `config.yaml > llm.default_profile`, fallback `"default"`).

**L'Atelier** lit `envelope.metadata.get("user_record", {}).get("llm_profile") or "default"` pour charger le `ProfileConfig` depuis `profiles.yaml`.

**La Sentinelle ne stampe jamais `llm_profile`** — elle transmet l'enveloppe inchangée.

---

### Fonctions `common/config_loader.py`

```python
from common.config_loader import get_relais_home, resolve_config_path, resolve_storage_dir

# Répertoire utilisateur (respect de RELAIS_HOME env var)
home = get_relais_home()            # ~/.relais  (ou $RELAIS_HOME si défini)

# Résolution de fichier de config avec cascade
path = resolve_config_path("portail.yaml")   # cascade: ~/.relais/config/ → /opt/relais/config/ → ./config/

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
- `config/*.yaml` (config, profiles, portail, sentinelle, mcp_servers, HEARTBEAT)
- `soul/SOUL.md` et variants (`SOUL_concise.md`, `SOUL_professional.md`)
- **Prompt templates canaux** (`telegram_default.md`, etc.)
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
├── user_record.py
│   └── (dépends: dataclasses — partagé Portail/Sentinelle/Atelier)
│
└── markdown_converter.py
    └── (dépends: re, html2text [opt])

portail/
└── user_registry.py
    ├── (dépends: yaml, pathlib, dataclasses)
    └── (uses: config_loader, user_record [portail.yaml — users + roles fusionnés])

Exports (utilisés par briques)
├── Envelope, PushEnvelope, MediaRef
├── AsyncRedis, create_redis_conn()
├── GracefulShutdown
├── initialize_user_dir()
├── ConfigLoader
├── UserRecord                    ← common/user_record.py (lecture seule pour Sentinelle/Atelier)
├── portail.UserRegistry          ← portail/user_registry.py (Portail seul)
├── convert_md_to_telegram()
├── convert_md_to_slack_mrkdwn()
├── strip_markdown()
└── markdown_to_html()
```

### Ordre de chargement recommandé

1. `config_loader` (basique, pas de dépendance)
2. `envelope` (structures)
3. `redis_client` (factory)
4. `user_record` (dataclass partagée)
5. `portail.user_registry` (Portail seulement — dépend config_loader + user_record)
6. `shutdown`, `markdown_converter` (indépendantes)
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

- **Aiguilleur/Discord** ✅ — discord.py bot (`aiguilleur/channels/discord/adapter.py`)
  - Gère mentions + DMs
  - **Streaming désactivé** (`streaming: false` dans `channels.yaml`) — réponse complète en un seul message
  - Méthodes internes de `_RelaisDiscordClient` :
    - `_ensure_consumer_group(stream, group)` — création idempotente du consumer group Redis
    - `_resolve_discord_channel(envelope)` — résolution canal/DM avec fallback `fetch_user + create_dm`
    - `_deliver_outgoing_message(data)` — parse + envoi du message final
    - `_consume_outgoing_stream()` — boucle de consommation `relais:messages:outgoing:discord`
    - `_typing_loop(channel)` — affiche l'indicateur "est en train d'écrire" dès réception d'un message (tâche asyncio, timeout 120 s)
    - `_cancel_typing()` — annule `_typing_loop` à l'envoi de la réponse

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
user sentinelle +@all ~relais:security ~relais:tasks ~relais:commands ~relais:messages:outgoing_pending ~relais:messages:outgoing:* ~relais:logs >$REDIS_PASS_SENTINELLE
user atelier +@all ~relais:tasks ~relais:messages:outgoing_pending ~relais:messages:streaming:* ~relais:logs >$REDIS_PASS_ATELIER
user souvenir +@all ~relais:memory:* ~relais:messages:outgoing:* ~relais:logs >$REDIS_PASS_SOUVENIR
```

### Sentinelle ACL

La Sentinelle ne résout **pas** l'identité utilisateur — elle reçoit `user_record` (dict `UserRecord`)
depuis `envelope.metadata["user_record"]`, stampé en amont par le Portail.
`sentinelle.yaml` contient uniquement `access_control` et `groups`.

**Deux modes globaux** (surchargeables par canal via `access_control.channels`) :
- `allowlist` (défaut) : enveloppes sans `user_record` valide rejetées. Groupes autorisés via `groups`.
- `blocklist` : tout admis sauf `user_record.blocked == true`.

**Mode permissif** : si aucun `sentinelle.yaml` n'est trouvé, l'ACL est désactivée avec un WARNING.

```yaml
# ~/.relais/config/sentinelle.yaml
access_control:
  default_mode: allowlist       # "allowlist" | "blocklist"
  channels:                     # Surcharges optionnelles par canal
    telegram:
      mode: blocklist

groups:                         # Groupes WhatsApp / Telegram (autorisation par group_id)
  - id: grp_famille
    channel: whatsapp
    group_id: "120363000000000@g.us"
    allowed: true
    blocked: false
```

### Guardrails LLM

Sentinelle filtre contenu via `ContentFilter` (pré et post-LLM) :

```python
# Avant appel LLM (check_input)
result = await content_filter.check_input(envelope.content, envelope.sender_id)
if not result.allowed:
    # Rejet — raison dans result.reason

# Après appel LLM (check_output)
result = await content_filter.check_output(response_text, envelope.sender_id)
if not result.allowed:
    # Blocage — raison dans result.reason
elif result.modified_text:
    # Réponse tronquée — utiliser result.modified_text
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

```
AgentExecutor.execute(envelope, context, stream_callback?):
    tools  ← internal_tools + MCP tools (all as BaseTool)
    agent  ← create_deep_agent(model, tools, system_prompt)
    for each chunk in agent.astream(messages, stream_mode="messages"):
        if stream_callback and chunk has content:
            await stream_callback(chunk.content)
        accumulate full_reply
    return full_reply
```

### Outils (LangChain `BaseTool`)

`AgentExecutor` accepte une liste de `BaseTool` (LangChain). Les outils internes et MCP sont unifiés sous ce type :

- **`ToolPolicy(base_dir)`** (`atelier/tool_policy.py`) — résout les répertoires de skills par rôle (`resolve_skills`), filtre les outils MCP par pattern (`filter_mcp_tools`) ; les dirs résolus sont passés comme `skills=` à `create_deep_agent()`
- **`make_mcp_tools(mcp_servers)`** (`atelier/mcp_adapter.py`) — charge les outils MCP via `langchain-mcp-adapters` et retourne des `BaseTool`
- Les outils MCP sont filtrés par `ToolPolicy.filter_mcp_tools()` avant d'être passés à l'agent

### Streaming Progressif

Atelier publie les tokens d'une réponse au fur et à mesure via `StreamPublisher` (token-by-token grâce à `agent.astream(stream_mode="messages")`) :

```
relais:messages:streaming:{channel}:{correlation_id}
├─ seq: 1, text: "Bonjour", is_final: 0
├─ seq: 2, text: "Bonjour, c'est", is_final: 0
├─ seq: 3, text: "Bonjour, c'est sympa", is_final: 1
└─ (Aiguilleur édite le message en temps réel)
```

**Throttling:** 80 caractères minimum entre éditions (rate limit 5 req/5s).

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
Résultat agrégé → relais:messages:outgoing_pending  (→ Sentinelle outgoing → outgoing:{channel})
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

## Le Commandant — Ajouter une commande

Le Commandant exécute les commandes slash hors-LLM pré-validées par La Sentinelle. Il consomme `relais:commands` (`commandant_group`) — toutes les enveloppes arrivant ici ont déjà passé l'ACL identité et l'ACL commande. Le Portail ne filtre plus les commandes.

### Architecture du registre

Deux sources de vérité complémentaires :

- **`common/command_utils.py`** — déclare `KNOWN_COMMANDS` (frozenset) : liste des commandes reconnues par La Sentinelle pour le routage vers `relais:commands`. **À mettre à jour en même temps que `commandant/commands.py`.**
- **`commandant/commands.py`** — déclare `COMMAND_REGISTRY` (handlers) : tout — handler, nom, description.

```python
# common/command_utils.py
KNOWN_COMMANDS: frozenset[str] = frozenset({"clear", "help"})
```

```python
# commandant/commands.py

@dataclass(frozen=True)
class CommandSpec:
    name: str                                      # Nom de la commande (ex: "clear")
    description: str                               # Description courte affichée par /help
    handler: Callable[..., Awaitable[None]]        # Coroutine exécutée à la détection

COMMAND_REGISTRY: dict[str, CommandSpec] = {
    "clear": CommandSpec(name="clear", description="...", handler=handle_clear),
    "help":  CommandSpec(name="help",  description="...", handler=handle_help),
}
```

`/help` construit sa réponse en itérant sur `COMMAND_REGISTRY.values()` — il se met à jour automatiquement quand une nouvelle commande est ajoutée.

### Ajouter une commande en 3 étapes

**Déclarer dans `common/command_utils.py`, implémenter dans `commandant/commands.py`, tester.**

**Exemple: ajouter `/status` qui retourne l'état du pipeline**

#### Étape 1 — Déclarer dans `common/command_utils.py`

```python
KNOWN_COMMANDS: frozenset[str] = frozenset({"clear", "help", "status"})
```

#### Étape 2 — Écrire le handler puis l'enregistrer (`commandant/commands.py`)

```python
# 1a. Définir le handler (avant COMMAND_REGISTRY dans le fichier)
async def handle_status(envelope: Envelope, redis_conn: Any) -> None:
    """Retourne l'état courant du pipeline.

    Args:
        envelope: L'enveloppe du message /status reçu.
        redis_conn: Connexion Redis async active.
    """
    task_len = await redis_conn.xlen("relais:tasks") or 0
    status_text = f"Pipeline actif — relais:tasks: {task_len} message(s) en attente."

    response = Envelope.from_parent(envelope, status_text)
    await redis_conn.xadd(
        f"relais:messages:outgoing:{envelope.channel}",
        {"payload": response.to_json()},
    )


# 2b. Ajouter l'entrée dans COMMAND_REGISTRY
COMMAND_REGISTRY: dict[str, CommandSpec] = {
    # ... entrées existantes ...
    "status": CommandSpec(
        name="status",
        description="Retourne l'état actuel du pipeline (streams actifs, bricks en ligne).",
        handler=handle_status,
    ),
}
```

#### Étape 3 — Écrire les tests (`tests/test_commandant.py`)

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_status_publishes_confirmation(mock_redis):
    from commandant.commands import handle_status
    envelope = Envelope(
        content="/status",
        sender_id="discord:123456",
        channel="discord",
        session_id="session_abc",
    )
    mock_redis.xlen = AsyncMock(return_value=0)
    await handle_status(envelope, mock_redis)

    expected_stream = "relais:messages:outgoing:discord"
    calls = [str(c) for c in mock_redis.xadd.call_args_list]
    assert any(expected_stream in c for c in calls)
```

### Règles de conception

| Règle | Raison |
|-------|--------|
| Toujours publier la réponse sur `relais:messages:outgoing:{envelope.channel}` | Canal de retour standard vers l'Aiguilleur |
| Utiliser `Envelope.from_parent(envelope, text)` pour la réponse | Préserve `session_id`, `correlation_id`, `channel` |
| Handler défini **avant** `COMMAND_REGISTRY` dans le fichier | Python évalue les noms au moment de la construction du dict |
| Actions Redis complexes → déléguer via stream | Ex: `/clear` → `XADD relais:memory:request {action: clear}` (Souvenir traite) |
| Déclarer dans `KNOWN_COMMANDS` **avant** tout test | La Sentinelle rejette les commandes absentes de `KNOWN_COMMANDS` |

### Permissions Redis du Commandant

```
user commandant on >pass_commandant ~relais:commands ~relais:messages:outgoing:* ~relais:memory:request ~relais:logs +@all
```

### Vérification

```bash
# Lancer les tests Commandant
pytest tests/test_commandant.py -v

# Vérifier que /help liste la nouvelle commande
# (test automatique via test_handle_help_lists_all_command_names)
pytest tests/test_commandant.py::test_handle_help_lists_all_command_names -v
```

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

1. Vérifier `~/.relais/config/portail.yaml` (utilisateurs/rôles) et `~/.relais/config/sentinelle.yaml` (ACL)
2. Logs Sentinelle: grep "acl_denied"
3. Message en DLQ, chercher raison

---

**Voir aussi:**
- [README.md](../README.md) — Démarrage rapide
- [CONTRIBUTING.md](CONTRIBUTING.md) — Dev workflow
- [plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md](../plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md) — Spécification complète
