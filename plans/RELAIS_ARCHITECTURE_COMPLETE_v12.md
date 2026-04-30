# RELAIS — Document d'Architecture

> **RELAIS** — *Station de relais : reçoit des messages de toutes origines,*
> *les achemine vers leur destination avec fiabilité et continuité.*
>
> Framework d'agents conversationnels multi-canaux, autonomes, extensibles,
> et auto-apprenants. Projet francophone, code anglais.

---

## Table des matières

1. Vision & Objectifs
2. Répertoire utilisateur — RELAIS_HOME
3. Convention de nommage
4. Taxonomie des briques
5. Les Briques — tableau d'ensemble
6. Infrastructure — supervisord & MCP servers
7. Le Coursier — Redis sécurisé
8. L'Aiguilleur — adaptateur de canaux & formatage Markdown
9. Le Portail — routage & politique de réponse
10. La Sentinelle — sécurité & profils
11. L'Atelier — exécution des agents, résilience LLM, outils internes
12. Le Souvenir — mémoire, fichiers persistants, archivage
13. L'Archiviste — logs & audit
14. Le Commandant — commandes globales hors-LLM
15. Corrélation end-to-end
16. SOUL.md — personnalité JARVIS & i18n
17. Profils — modélisation complète
18. Politique de réponse automatique
19. Gestion des médias
20. Système d'extensions
21. Sécurité
22. Structure du projet
23. La Charte RELAIS
24. Planifié — Briques futures

---

## 1. Vision & Objectifs

### Problème résolu

RELAIS centralise tous les canaux de communication vers un système d'agents LLM unique, configurable et extensible. Une seule personnalité (JARVIS), une seule mémoire, une seule configuration, quel que soit le canal.

### Principes fondateurs

- **Une brique = une responsabilité** — isolation totale, testabilité maximale
- **Tout est configuration** — pas de code pour changer un comportement métier
- **supervisord gère les processus** — pas Python, pas de sous-processus manuels
- **Redis est le seul bus** — zéro appel HTTP direct entre briques
- **Extensible par des tiers** — intercepteurs in-process, observers Redis out-of-process
- **Robuste par conception** — graceful shutdown, Redis Streams, at-least-once delivery

### Cible de déploiement initial

Mac Mini M4 Pro 48 Go — machine dédiée, toujours allumée. Migration Linux/VPS transparente : supervisord → systemd sans toucher au code métier.

---

## 2. Répertoire utilisateur — RELAIS_HOME

### Convention

Toute la configuration personnalisée, les skills, les logs et les médias sont stockés dans `RELAIS_HOME`, résolu par `common/config_loader.py`.

Comportement actuel du code :
- si `RELAIS_HOME` est défini, il est utilisé tel quel
- sinon, le défaut est `./.relais` à la racine du repo

La documentation ci-dessous décrit donc le répertoire logique `RELAIS_HOME`, sans supposer un chemin local particulier.

```
<RELAIS_HOME>/
│
├── config/
│   ├── config.yaml               ← surcharge <INSTALL_CONFIG_DIR>/config/config.yaml
│   ├── atelier/
│   │   ├── profiles.yaml         ← profils personnalisés
│   │   └── mcp_servers.yaml      ← MCP servers additionnels
│   ├── portail.yaml              ← registry utilisateurs + rôles (Portail)
│   ├── sentinelle.yaml           ← ACL Sentinelle (access_control + groups)
│   ├── atelier.yaml              ← config comportementale Atelier (progress events)
│   └── HEARTBEAT.md              ← tâches planifiées personnalisées
│
├── prompts/
│   ├── soul/
│   │   ├── SOUL.md               ← personnalité chargée par SoulAssembler
│   │   └── variants/
│   │       ├── SOUL_concise.md
│   │       └── SOUL_professional.md
│   ├── channels/
│   ├── policies/
│   ├── roles/
│   └── users/
│
├── skills/
│   └── CLAUDE.md                 ← registre créé par initialize_user_dir()
│
├── media/                        ← fichiers médias temporaires (TTL 24h)
│
├── logs/                         ← L'Archiviste écrit ici
│   └── events.jsonl
│
├── storage/
│   └── memory.db                 ← SQLite Souvenir
│
└── backup/                       ← backups locaux (si backup.path non configuré)
```

### Cascade de résolution

```
1. RELAIS_HOME/config/   ← config utilisateur (priorité maximale)
2. <INSTALL_CONFIG_DIR>/config/   ← installation système
3. ./config/             ← répertoire courant (mode dev)
```

La variable `RELAIS_HOME` permet de surcharger explicitement le répertoire de travail (ex: `/srv/relais` en production, `/tmp/relais-test` pour les tests d'intégration).

### Initialisation au premier lancement

Au démarrage, RELAIS crée automatiquement `RELAIS_HOME/` et y copie les fichiers `.default` disponibles depuis l'installation courante. Les fichiers déjà présents ne sont jamais écrasés — l'opération est idempotente.

### Impact par brique

| Brique | Ce qui change |
|---|---|
| L'Archiviste | Écrit dans `RELAIS_HOME/logs/` |
| L'Atelier | Charge les skills depuis `RELAIS_HOME/skills/` |
| Le Souvenir | DB dans `RELAIS_HOME/storage/memory.db` |
| Le Portail | Charge `RELAIS_HOME/config/portail.yaml` |
| SoulAssembler | Charge `RELAIS_HOME/prompts/` si présent |
| Le Veilleur | Planifié ; lirait `RELAIS_HOME/config/HEARTBEAT.md` |
| Tous | Config chargée via cascade automatique |

---

## 3. Convention de nommage

```
┌─────────────────────────────────────────────────────────────────┐
│  Code (variables, méthodes, classes, fichiers) → ANGLAIS        │
│  Noms des briques fonctionnelles               → FRANÇAIS        │
│  Documentation et commentaires                 → FRANÇAIS        │
│  Clés de fichiers de configuration YAML        → ANGLAIS        │
│  Contenu de SOUL.md et des prompts             → FRANÇAIS        │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. Taxonomie des briques

```
┌──────────────────────┬──────────────────────────────────────────┐
│ PURE OBSERVER        │ Souscrit Redis, écrit vers externe        │
│                      │ → L'Archiviste  (logs → JSONL + SQLite)  │
│                      │ → Le Scrutateur (events → Prometheus)     │
├──────────────────────┼──────────────────────────────────────────┤
│ BATCH PROCESSOR      │ Lancé 1×/jour, lit SQLite, publie, exit  │
│                      │ → Le Forgeron (SQLite → skills:new)      │
├──────────────────────┼──────────────────────────────────────────┤
│ PURE PUBLISHER       │ Publie Redis uniquement, aucun LLM       │
│                      │ → Le Veilleur (heartbeat → tasks Stream) │
├──────────────────────┼──────────────────────────────────────────┤
│ TRANSFORMER          │ Souscrit Redis + republish Redis         │
│                      │ → Le Portail, La Sentinelle, Le Crieur   │
├──────────────────────┼──────────────────────────────────────────┤
│ STREAM CONSUMER      │ Consomme Stream, exécute, répond         │
│                      │ → L'Atelier, Le Souvenir                 │
├──────────────────────┼──────────────────────────────────────────┤
│ RELAY                │ Canal externe ↔ Redis                    │
│                      │ → L'Aiguilleur (processus unifié)        │
├──────────────────────┼──────────────────────────────────────────┤
│ ADMIN                │ Pilote supervisord + Redis               │
│                      │ → Le Vigile, Le Tableau                  │
├──────────────────────┼──────────────────────────────────────────┤
│ INTERCEPTOR CHAIN    │ In-process dans L'Atelier                │
│                      │ → Le Tisserand                           │
└──────────────────────┴──────────────────────────────────────────┘
```

---

## 5. Les Briques — tableau d'ensemble

### Briques implémentées

| Brique | Module | Taxonomie | Rôle | Streams |
|---|---|---|---|---|
| 🚦 **L'Aiguilleur** | `aiguilleur/` | Relay | Adaptateur de canaux — processus unifié | ← `relais:messages:outgoing:{channel}` / → `relais:messages:incoming` |
| 🏛️ **Le Portail** | `portail/` | Transformer | Résolution d'identité et enrichissement `user_record` | ← `relais:messages:incoming` / → `relais:security` |
| 🛡️ **La Sentinelle** | `sentinelle/` | Transformer | ACL, routage commandes/messages, pass-through sortant | ← `relais:security` / → `relais:tasks`,`relais:commands` ; ← `relais:messages:outgoing_pending` / → `relais:messages:outgoing:{channel}` |
| 📨 **Le Coursier** | Redis | Infrastructure | Bus messages Unix socket | — |
| ⚒️ **L'Atelier** | `atelier/` | Stream Consumer | Exécution agents LLM | ← `relais:tasks` / → `relais:messages:outgoing_pending`,`relais:messages:streaming:{channel}:{corr}`,`relais:tasks:failed`,`relais:memory:request`,`relais:logs` |
| 💭 **Le Souvenir** | `souvenir/` | Stream Consumer | Contexte Redis, archivage SQLite, fichiers mémoire persistants | ← `relais:memory:request` + `relais:messages:outgoing:{channel}` / → `relais:memory:response` |
| 📚 **L'Archiviste** | `archiviste/` | Pure Observer | Audit JSONL + observation pipeline | ← `relais:logs`,`relais:events:*`,`relais:messages:incoming`,`relais:security`,`relais:tasks`,`relais:tasks:failed`,`relais:messages:outgoing:discord` |

### Briques planifiées

Voir section 23 pour le détail fonctionnel.

| Brique | Module | Taxonomie | Rôle |
|---|---|---|---|
| 🌙 **Le Veilleur** | `veilleur/` | Pure Publisher | CRON + Heartbeat + backup |
| 🔧 **Le Forgeron** | `forgeron/` | Batch Processor | Génération skills auto |
| 📣 **Le Crieur** | `crieur/` | Transformer | Push proactif multi-canal |
| 🔱 **Le Vigile** | `vigile/` | Admin | NLP → supervisord + hot reload |
| 📊 **Le Tableau** | `tableau/` | Admin + Relay | TUI bidirectionnel |
| 🧵 **Le Tisserand** | `tisserand/` | Interceptor Chain | Extensions in-process |
| 🔍 **Le Scrutateur** | `scrutateur/` | Pure Observer | Prometheus + Loki + ES |
| 🎮 **Le Commandant** | `commandant/` | Transformer | Commandes globales hors-LLM (`/clear`, `/help`) |

---

## 6. Infrastructure — supervisord & MCP servers

### Ordre de démarrage

| Priorité | Groupe | Briques |
|---|---|---|
| 1 | `infra` | Le Coursier (Redis) |
| 8 | `observers` | L'Archiviste |
| 10 | `core` | Le Portail, La Sentinelle, L'Atelier, Le Souvenir, **Le Commandant** |
| 20 | `relays` | L'Aiguilleur (processus unifié) |
| 30 | — | Le Tableau (local, à la demande, `autostart=false`) |

Toutes les briques loggent dans `RELAIS_HOME/logs/` via `stdout_logfile` supervisord.

### MCP servers lifecycle — modèle hybride

```
MCP GLOBAUX — supervisord (processus persistants)
  Toujours disponibles, légers, indépendants du contexte
  Démarrent avec RELAIS, vivent toute la durée de vie du système

  Ex: mcp-calendar, mcp-brave-search

MCP CONTEXTUELS — spawned par L'Atelier (à la demande)
  Liés à un profil ou un contexte spécifique
  Spawned pour chaque session, tués en fin de tâche

  Ex: mcp__jcodemunch, mcp__gitlab
```

### config/atelier/mcp_servers.yaml

Format canonique — deux transports supportés : `stdio` (sous-processus spawné par l'Atelier) et `sse` (connexion à un serveur HTTP existant).

```yaml
mcp_servers:

  # Serveurs globaux — disponibles pour tous les profils
  global:
    - name: filesystem
      enabled: true
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    - name: calendar
      enabled: false
      type: sse
      url: "http://127.0.0.1:8100"

  # Serveurs contextuels — activés selon le profil actif
  contextual:
```

> **Note as-built :** le cycle de vie décrit ici correspond au loader et au session manager actuels. Les exemples `mcp-calendar`, `mcp-brave-search`, `mcp__jcodemunch`, `mcp__gitlab` sont illustratifs ; la source de vérité des serveurs réellement actifs reste `config/atelier/mcp_servers.yaml`.
    - name: code-tools
      enabled: true
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-github"]
      profiles: [coder, precise]
      env:
        GITHUB_TOKEN: "${GITHUB_TOKEN}"
```

> **Sélection :** `global` → inclus si `enabled: true`. `contextual` → inclus si `enabled: true` ET profil actif dans `profiles`.
>
> **Filtrage MCP par rôle** — les outils MCP exposés au modèle sont filtrés par `ToolPolicy.filter_mcp_tools()` selon les patterns `allowed_mcp_tools` définis dans `portail.yaml:roles:`. Les champs `shell_timeout_seconds` (défaut 30 s) et `max_turn_seconds` (défaut 300 s) sont des champs de `ProfileConfig` qui contrôlent respectivement le timeout par appel shell et le timeout total du tour agentique. `mcp_timeout` et `mcp_max_tools` ont été supprimés lors de la migration DeepAgents.

---

## 7. Le Coursier — Redis sécurisé

### config/redis.conf

```ini
port 0
unixsocket /var/run/relais/redis.sock
unixsocketperm 770
requirepass ${REDIS_PASSWORD}

; ACL par brique
user aiguilleur on >${REDIS_PASS_AIGUILLEUR}
  ~relais:messages:* ~relais:active_sessions:*
  +subscribe +publish +hget

user portail    on >${REDIS_PASS_PORTAIL}
  ~relais:messages:* ~relais:security ~relais:tasks ~relais:active_sessions:* ~relais:logs
  +subscribe +publish +xadd +hset +expire

user sentinelle on >${REDIS_PASS_SENTINELLE}
  ~relais:security ~relais:tasks ~relais:messages:outgoing_pending ~relais:messages:outgoing:* ~relais:logs
  +subscribe +publish +xreadgroup +xack +xadd

user atelier    on >${REDIS_PASS_ATELIER}
  ~relais:tasks ~relais:tasks:failed ~relais:memory:* ~relais:messages:streaming:* ~relais:messages:outgoing_pending ~relais:events:* ~relais:logs
  +subscribe +publish +xreadgroup +xack +xadd

user souvenir   on >${REDIS_PASS_SOUVENIR}
  ~relais:memory:* ~relais:sessions:* ~relais:context:* ~relais:messages:outgoing:* ~relais:logs
  +subscribe +publish +get +set +expire +rpush +ltrim +lrange +xreadgroup +xack +xadd

user veilleur   on >${REDIS_PASS_VEILLEUR}
  ~relais:tasks ~relais:push:* ~relais:logs
  +subscribe +publish +xadd +get +set

user crieur     on >${REDIS_PASS_CRIEUR}
  ~relais:push:* ~relais:notifications:* ~relais:active_sessions:* ~relais:messages:*
  +subscribe +psubscribe +publish +hgetall +expire

user forgeron   on >${REDIS_PASS_FORGERON}
  ~relais:skills:* ~relais:logs
  +publish

user archiviste on >${REDIS_PASS_ARCHIVISTE}
  ~relais:logs ~relais:events:*
  +subscribe +psubscribe +xreadgroup +xack

user vigile     on >${REDIS_PASS_VIGILE}
  ~relais:admin:* ~relais:push:* ~relais:logs
  +subscribe +psubscribe +publish

user scrutateur on >${REDIS_PASS_SCRUTATEUR}
  ~relais:events:* ~relais:logs
  +subscribe +psubscribe

user tisserand  on >${REDIS_PASS_TISSERAND}
  ~relais:events:* ~relais:logs
  +subscribe +psubscribe +publish

user default    off
```

### Topics Redis — mapping définitif

```
STREAMS (at-least-once — perte inacceptable)
  relais:tasks                          Portail/Veilleur → Atelier
  relais:tasks:failed                   Atelier → DLQ (AgentExecutionError exhausted)
  relais:memory:request                 Atelier → Souvenir
  relais:memory:response                Souvenir → Atelier
  relais:messages:incoming              Aiguilleur → Portail
  relais:messages:outgoing_pending      Atelier → Sentinelle (outgoing pass-through)
  relais:messages:outgoing:{channel}    Sentinelle → Aiguilleur + Souvenir (observer)
  relais:messages:streaming:{ch}:{corr} Atelier → Aiguilleur (progressive chunks)
  relais:security                       Portail ↔ Sentinelle
  relais:logs                           Toutes → Archiviste (audit critique)
  relais:skills:new                     Forgeron → Vigile

LISTS (fast cache — volatile)
  relais:context:{session_id}           Souvenir stores (RPUSH/LTRIM), Atelier reads

HASHES / KEYS
  relais:active_sessions:{sender_id}    Portail stores (HSET + TTL 1h)

PUB/SUB (fire & forget — perte acceptable)
  relais:streaming:start:{channel}      Atelier → adaptateur du canal
  relais:events:system                  Événements système observés par Archiviste
  relais:events:messages                Événements messages observés par Archiviste
  relais:push:{urgency}                 Planifié — Crieur
  relais:notifications:{role}           Planifié — Crieur
  relais:admin:*                        Planifié — Vigile
```

---

## 8. L'Aiguilleur — adaptateur de canaux & formatage Markdown

### Architecture — processus unifié

L'Aiguilleur est un **processus unique** (`aiguilleur/main.py`) qui gère tous les adaptateurs de canaux. L'`AiguilleurManager` charge les canaux depuis `aiguilleur.yaml` et instancie les adaptateurs au démarrage.

- **Adaptateurs natifs** (`type: native`) — thread Python + `asyncio.run`, ex: `DiscordAiguilleur`
- **Adaptateurs externes** (`type: external`) — `subprocess.Popen`, pour les adaptateurs non-Python
- **Restart automatique** — backoff exponentiel `min(2^restart_count, 30)` secondes, `max_restarts` configurable
- **Découverte automatique** — convention `aiguilleur.channels.{name}.adapter` (cherche la classe dont le nom se termine par `Aiguilleur`), surchargeable via `class_path`

### État d'implémentation des canaux

Le repo contient aujourd'hui un seul adaptateur concret : `aiguilleur/channels/discord/adapter.py`.

Les autres canaux visibles dans `aiguilleur.yaml.default` sont des cibles de configuration ou des placeholders de supervision ; ils ne correspondent pas à des adaptateurs présents dans le code à ce stade.

### Configuration des canaux via `aiguilleur.yaml`

```yaml
channels:
  discord:
    enabled: true                    # Activé/désactivé
    streaming: false                 # Discord final-only dans l'implémentation actuelle
    type: native                     # "native" (Python, thread+asyncio) | "external" (subprocess)
    class_path: null                 # Override : "aiguilleur.channels.discord.adapter.DiscordAiguilleur"
    max_restarts: 5
    # profile non défini → fallback config.yaml > llm.default_profile

  telegram:
    enabled: false
    streaming: true
    type: native
    class_path: null
    max_restarts: 5
    profile: fast                    # Profil LLM imposé à tous les messages de ce canal

  slack:
    enabled: false
    streaming: false
    type: native
    class_path: null
    max_restarts: 5

  rest:
    enabled: false
    streaming: false
    type: native
    class_path: null
    max_restarts: 5

  tui:
    enabled: false
    streaming: true
    type: native
    class_path: null
    max_restarts: 5

  whatsapp:
    enabled: false
    streaming: false
    profile: default
    prompt_path: "channels/whatsapp_default.md"
    max_restarts: 5
    # L'adaptateur WhatsApp est un NativeAiguilleur Python qui héberge un
    # serveur webhook aiohttp et dialogue avec la passerelle baileys-api
    # (Node.js, lancée par scripts/run_baileys.py sous supervisord, programme
    # `baileys-api` dans le groupe `optional`).
```

**Paramètres clés :**
- `enabled` — toggle sans suppression de code
- `streaming` — flag utilisé par L'Atelier pour `STREAMING_CAPABLE_CHANNELS`
- `type` — `native` (thread Python + asyncio) ou `external` (subprocess)
- `class_path` — override de la classe adaptateur
- `max_restarts` — max avant abandon, restart avec backoff exponentiel
- `profile` — profil LLM appliqué à tous les messages du canal (optionnel) ; stampé dans `envelope.context["aiguilleur"]["channel_profile"]` par l'Aiguilleur au moment de la création de l'enveloppe entrante ; si absent, l'Aiguilleur lit `config.yaml > llm.default_profile` (fallback `"default"`)

> **Responsabilité du stamping :** c'est l'**Aiguilleur** (adaptateur de canal) qui stampe `envelope.context["aiguilleur"]["channel_profile"]`. Le Portail stampe ensuite le profil effectif dans `envelope.context["portail"]["llm_profile"]` (depuis `channel_profile` ou `"default"`). La Sentinelle n'écrit jamais ce champ.
- `command`/`args` — requis pour `type: external` uniquement

### Tableau des canaux

| Canal | Statut repo | Notes |
|---|---|---|
| Discord | Implémenté | `discord.py`, réception mentions/DM, indicateur de frappe, sortie finale |
| Telegram | Placeholder config | Pas d'adaptateur présent dans `aiguilleur/channels/` |
| Slack | Placeholder config | Pas d'adaptateur présent dans `aiguilleur/channels/` |
| REST | Implémenté | `aiguilleur/channels/rest/` — FastAPI/aiohttp, auth Bearer, endpoints : `POST /v1/message`, `GET /v1/stream/{correlation_id}` (SSE), `GET /v1/events` (SSE fan-out), `GET /v1/commands` (catalogue CQRS → Commandant via `relais:commandant:query`) |
| TUI | Implémenté (TypeScript) | `tools/tui-ts/` — TUI TypeScript React Ink, auto-complétion via `GET /v1/commands` |
| WhatsApp | Implémenté (2026-04-10) | Adaptateur Python natif (`aiguilleur/channels/whatsapp/adapter.py`) — serveur webhook aiohttp + client REST vers la passerelle externe [fazer-ai/baileys-api](https://github.com/fazer-ai/baileys-api) (Node.js, programme supervisord `baileys-api` dans le groupe `optional`). Installation, config et pairing QR pris en charge par le sous-agent `relais-config` via les tools LangChain `whatsapp_install`, `whatsapp_configure`, `whatsapp_uninstall` (chargés via `tool_tokens: [module:aiguilleur.channels.whatsapp.tools]`). CLI : `python -m aiguilleur.channels.whatsapp`. Voir `docs/WHATSAPP_SETUP.md` et `plans/WHATSAPP_ADAPTER.md`. |

### Streaming progressif — édition temps réel

L'Atelier publie les chunks token-by-token et les événements de progression dans `relais:messages:streaming:{channel}:{correlation_id}` via `StreamPublisher`.

Dans l'état actuel du code :
- Discord n'édite pas de message en temps réel ; il envoie une réponse finale sur `relais:messages:outgoing:discord`
- l'adaptateur Discord maintient un indicateur de frappe tant que la requête est en vol
- `StreamPublisher.push_progress()` publie aussi des enveloppes `message_type=progress` sur `relais:messages:outgoing:{channel}` pour les canaux non-streaming
- un signal Pub/Sub `relais:streaming:start:{channel}` est émis au début d'un stream

Chaque entrée du stream de streaming contient :
- `type=token` ou `type=progress`
- `seq`
- `is_final`

---

## 9. Le Portail — routage & politique de réponse

### Rôle

Le Portail valide le format Envelope entrant, résout l'utilisateur via `UserRegistry` (`portail.yaml`), enrichit l'enveloppe avec une identité consolidée, met à jour le registre des sessions actives, puis route tout message accepté vers La Sentinelle.

Champs réellement écrits aujourd'hui :
- `metadata.user_record` — dict sérialisé `UserRecord`
- `metadata.user_id` — raccourci stable cross-canal

Le Portail n'écrit pas de clés top-level `user_role`, `display_name`, `custom_prompt_path`, `skills_dirs` ou `allowed_mcp_tools` ; ces données vivent dans `metadata.user_record`.

**`unknown_user_policy`** (champ top-level de `portail.yaml`) :
- `deny` (défaut) : drop silencieux
- `guest` : stamp identité guest synthétique, rôle `guest` (accès restreint)
- `pending` : publie l'enveloppe sur `relais:admin:pending_users` pour validation manuelle, puis drop

---

## 10. La Sentinelle — sécurité & profils

### Séparation des responsabilités : enrichissement vs sécurité

**Architecture clarifiée :**

1. **Le Portail** effectue l'**enrichissement contextuel** : résout l'utilisateur depuis `portail.yaml`, stampe `envelope.metadata["user_record"]` (dict `UserRecord` fusionné — rôle + utilisateur, incluant `user_id`) et `envelope.metadata["user_id"]` (raccourci stable cross-canal, égal à la clé YAML, ex : `"usr_admin"`). Portail est le **seul écrivain** de l'identité utilisateur. `user_id` permet aux briques aval (Souvenir, Atelier) de reprendre une conversation quelque soit le canal d'origine sans connaître le `sender_id` spécifique au canal.
2. **La Sentinelle** effectue la **sécurité pure et le routage des commandes** : lit `user_record` depuis `envelope.context["portail"]` (chargé par Portail), bifurque commandes et messages normaux, puis republie le flux sortant. Config ACL dans `sentinelle.yaml`.

> **Note architecture :** La Sentinelle n'effectue **pas** d'enrichissement.
> En flux entrant, elle valide l'ACL et bifurque :
> - **Message normal** → publie vers `relais:tasks`
> - **Commande connue + autorisée** (`action=<cmd>` dans le rôle) → publie vers `relais:commands`
> - **Commande inconnue** → réponse inline `"Commande inconnue : /xxx"`, ACK sans forward
> - **Commande non autorisée** → réponse inline `"Vous n'avez pas la permission d'exécuter /xxx"`, ACK sans forward
>
> En flux sortant, elle consomme `relais:messages:outgoing_pending`, applique actuellement un pass-through, et publie vers `relais:messages:outgoing:{channel}`.

> **Divergence connue :** le pipeline porte aujourd'hui une incohérence de vocabulaire entre `server` côté Discord et `group` côté Sentinelle/ACL. Le document décrit donc seulement le comportement observé, sans affirmer une matrice contextuelle harmonisée tant que le code n'est pas unifié.

### Les 3 rôles humains

```
ADMIN       → Accès total. supervisord complet. Tous tools/MCP.
              Reçoit toutes les notifications.

SUPERVISOR  → Lecture système. Restart aiguilleurs uniquement.
              Tools : Read + bash(git/docker). MCP : gitlab, brave, jCodeMunch.
              Reçoit notifications système high + critical.

USER        → Conversation standard. Read uniquement. Pas de bash.
              Reçoit ses propres notifications de tâches uniquement.
```

### config/portail.yaml

Schéma dict-keyed (clé = `usr_<id>`) avec identifiants contextuels par canal.

```yaml
access_control:
  default_mode: allowlist       # "allowlist" | "blocklist"
  channels:                     # Surcharges optionnelles par canal
    # telegram:
    #   mode: blocklist

groups:                         # Groupes WhatsApp / Telegram — auth par group_id
  - channel: whatsapp
    group_id: "120363000000000@g.us"
    allowed: true
    blocked: false

users:
  usr_benjamin:
    display_name: "Benjamin"
    role: admin
    blocked: false
    identifiers:
      discord:
        dm: "789012345678"          # accès DM
        server: "789012345678"      # accès mentions serveur (null = interdit)
      telegram:
        dm: "123456789"
      rest:
        api_keys: ["clé-api-1"]     # clés API REST (texte clair dans portail.yaml)

  usr_system:
    display_name: "RELAIS System"
    role: admin
    blocked: false
    notes: "Compte interne — ne pas supprimer"

roles:
  admin:
    actions: ["*"]       # "*" = toutes les commandes slash autorisées
  user:
    actions: []          # [] = aucune commande slash autorisée (messages normaux OK via default_mode)
```

### Politique utilisateur inconnu

La politique est portée par `UserRegistry` via `portail.yaml`.

```
unknown_user_policy: "deny"     # rejet silencieux (défaut)
unknown_user_policy: "guest"    # accès avec rôle guest synthétique
unknown_user_policy: "pending"  # rejet + notification dans relais:admin:pending_users
```

---

## 11. L'Atelier — exécution des agents, résilience LLM, outils internes

### Architecture générale

L'Atelier suit ce flux pour chaque tâche entrante :

```
Incoming envelope
  ↓
Parse + load profile
  — lit envelope.context["portail"]["llm_profile"] (stampé par le Portail)
  — si absent : fallback "default"
  — résout le ProfileConfig depuis atelier/profiles.yaml (model, max_turns, max_tokens, resilience)
  ↓
Request context from Souvenir (relais:memory:request stream)
  ↓
Assemble system prompt (SOUL + role + user + channel)
  ↓
Load MCP servers for profile (mcp_loader.load_for_sdk)
  ↓
Build skills list (ToolPolicy.resolve_skills → deepagents skills= natif)
  ↓
Execute via AgentExecutor (boucle multi-tour explicite)
  ├─ Start MCP servers (stdio/SSE)
  ├─ Merge internal tools + MCP tools
  └─ Loop: stream → tool calls → results → next turn, until end_turn or max_turns
  ↓
If streaming capable: publish chunks to relais:messages:streaming:{channel}:{correlation_id}
  ↓
Publish response to relais:messages:outgoing_pending  (→ Sentinelle outgoing → outgoing:{channel})
  ↓
Conditional XACK (success or DLQ) — never lose messages on retry
```

### Modules — atelier/

```
atelier/
├── main.py                 # Brique principale — loop Redis, dispatch
├── agent_executor.py       # AgentExecutor : boucle agentique multi-tour
├── mcp_adapter.py          # make_mcp_tools() → outils MCP via langchain-mcp-adapters
├── mcp_session_manager.py  # Cycle de vie des serveurs MCP
├── tool_policy.py          # ToolPolicy — résolution skills_dirs + filtrage MCP (fail-closed)
├── mcp_loader.py           # Chargement config MCP servers
├── soul_assembler.py       # Assemblage prompt système
└── stream_publisher.py     # Publication chunks Redis
```

### Boucle agentique

L'`AgentExecutor` gère un cycle multi-tour via `deepagents.create_deep_agent()` :
1. Construction des messages (contexte court-terme + envelope)
2. Démarrage des serveurs MCP et chargement des outils
3. Appel LLM avec streaming token-by-token (`agent.astream(stream_mode="messages")`)
4. Dispatch des tool calls ; injection des résultats (`tool_result`)
5. Rebouclage jusqu'à `end_turn` ou `max_turns`

**Loop guard** : `ToolErrorGuard` surveille les erreurs d'outils — si le même outil nommé retourne `status="error"` 5 fois consécutives, ou si 8 erreurs totales sont atteintes, `AgentExecutor.execute()` lève `AgentExecutionError` pour interrompre la requête et éviter les boucles infinies. Le seuil total (8) est volontairement supérieur au seuil consécutif (5) : le prompt système inclut des instructions de self-diagnostic qui poussent l'agent à relire les sections troubleshooting du SKILL.md après des erreurs répétées, ce qui nécessite quelques tentatives supplémentaires. Sur `AgentExecutionError`, l'état partiel de la conversation est capturé dans `exc.messages_raw` et transmis à `ErrorSynthesizer` (réponse d'erreur visible par l'utilisateur) et à Forgeron (trace avec le contexte conversationnel complet). Les outils sans nom (`name == "?"`) sont exclus du comptage consécutif pour éviter les faux positifs.

### Outils skills — ToolPolicy + deepagents natif

`atelier/tool_policy.py` centralise l'accès aux skills et aux outils MCP :

| Méthode | Description |
|---------|-------------|
| `resolve_skills(metadata_value)` | Retourne les chemins absolus des skills_dirs autorisés (guard path traversal) |
| `filter_mcp_tools(tools, metadata_value)` | Filtre la liste d'outils MCP selon les patterns fnmatch (fail-closed) |

Les chemins resolus sont passés directement à `create_deep_agent(skills=[...])` — deepagents gère nativement la discovery des skills (plus de `list_skills`/`read_skill` explicites).

### Serveurs MCP — McpSessionManager

`McpSessionManager` prend en charge deux transports : `stdio` (sous-processus) et `sse` (connexion HTTP). Si le package MCP est absent, le manager loggue un warning et retourne des listes vides sans crash — les outils internes restent fonctionnels dans tous les cas.

### ProfileConfig — champs clés

| Champ | Type | Description |
|-------|------|-------------|
| `model` | str | Format `provider:model-id` (ex: `anthropic:claude-sonnet-4-6`) |
| `temperature` | float | Température LLM |
| `max_tokens` | int | Tokens max par réponse |
| `max_turns` | int | Tours max boucle agentique (défaut: 20) |
| `base_url` | str \| None | Endpoint LLM custom (LM Studio, Ollama, etc.) ; supporte `${VAR}` |
| `api_key_env` | str \| None | Nom de la variable d'env contenant la clé API |
| `resilience.retry_attempts` | int | Nombre de retries configurés |
| `resilience.retry_delays` | list[int] | Délais de retry |
| `resilience.fallback_model` | str \| None | Fallback déclaré dans la section résilience |
| `fallback_model` | str \| None | Fallback déclaré au niveau du profil |
| `mcp_timeout` | int | Timeout d'un appel MCP |
| `mcp_max_tools` | int | Nombre max d'outils MCP exposés |
| `parallel_tool_calls` | bool \| None | Transmet le paramètre OpenAI-compatible `parallel_tool_calls` au modèle. `False` désactive les appels parallèles (utile pour Mistral qui émet des appels parallèles incorrects). `None` (défaut) : le provider décide. |

> `allowed_tools`, `allowed_mcp`, `guardrails`, `memory_scope` sont retirés de `ProfileConfig` — voir `portail.yaml:roles:` pour le contrôle d'accès par rôle.

### Résilience LLM — pattern XACK

```yaml
# config/atelier/profiles.yaml — section résilience dans chaque profil
default:
  model: anthropic:claude-opus-4-6
  max_turns: 20
  temperature: 0.7
  max_tokens: 2048
  resilience:
    retry_attempts: 3
    retry_delays: [2, 5, 15]   # délais en secondes, backoff exponentiel
    fallback_model: null
```

**Règle fondamentale :** ne jamais XACK avant le succès ou l'épuisement des retries.

- `AgentExecutionError` → `ErrorSynthesizer` produit une réponse d'erreur empathique via appel LLM léger (historique partiel dans `exc.messages_raw`) → publiée sur `relais:messages:outgoing_pending` → DLQ (`relais:tasks:failed`) + ACK
- Exception transiente (réseau, timeout) → pas d'ACK → reste en PEL pour re-livraison
- Succès → ACK après publication dans `relais:messages:outgoing_pending`

---

## 12. Le Souvenir — mémoire, dual-stream, fichiers persistants

### Trois niveaux de mémoire

```
Mémoire contexte  → Redis (volatile, TTL 24h)
                    Cache rapide pour l'historique conversationnel
                    Clé : relais:context:{session_id} (List Redis)

Mémoire longue    → SQLite RELAIS_HOME/storage/memory.db
                    Messages archivés
                    Table: archived_messages

Fichiers mémoire  → SQLite RELAIS_HOME/storage/memory.db
                    Fichiers persistants de l'agent (chemins virtuels /memories/...)
                    Table: memory_files (isolés par user_id)
```

### Architecture dual-stream

Le Souvenir consomme deux streams en parallèle :

**Stream 1 : `relais:memory:request`** (Atelier → Souvenir)
- Action `get` : retourner l'historique pour une session donnée
- Action `file_write` : créer/écraser un fichier mémoire (`overwrite` flag)
- Action `file_read` : lire le contenu d'un fichier mémoire
- Action `file_list` : lister les fichiers mémoire sous un préfixe de chemin
- Flux : Atelier envoie le payload JSON → Souvenir répond via `relais:memory:response`

**Stream 2 : `relais:messages:outgoing:{channel}`** (observe toutes les réponses)
- Pour chaque message sortant : mettre en cache Redis et archiver en SQLite

### Flux mémoire : get (Atelier → Souvenir → Atelier)

```
1. Atelier → XADD relais:memory:request {action: "get", session_id, correlation_id}

2. Souvenir._handle_get_request():
   a. Try Redis List relais:context:{session_id} (cache, fast path)
   b. If miss → SQLite SELECT (fallback on Redis restart)
   c. XADD relais:memory:response {correlation_id, messages: [...]}

3. Atelier ← XREAD relais:memory:response (timeout 3s, filter by correlation_id)
```

### Flux mémoire : file_write/file_read/file_list (SouvenirBackend → Souvenir)

```
SouvenirBackend (atelier/souvenir_backend.py, opérations synchrones appelées depuis l'agent)
  ↓
redis_sync.Redis.xadd(relais:memory:request, {action, user_id, path, content, correlation_id})
  ↓
Souvenir.FileWriteHandler / FileReadHandler / FileListHandler
  ↓
FileStore._write_file / _read_file / _list_files (SQLite table memory_files)
  ↓
redis.xadd(relais:memory:response, {correlation_id, ok, content/files/error})
  ↓
SouvenirBackend polls relais:memory:response (timeout 3s) → returns WriteResult/str/list[FileInfo]
```

**CompositeBackend routing dans Atelier :**
- Chemin `/memories/...` → `SouvenirBackend` (SQLite via Souvenir)
- Autres chemins → `StateBackend` (état en mémoire, durée de la tâche)

### Flux mémoire : outgoing (observation + archivage)

```
1. relais:messages:outgoing:{channel} published by Sentinelle

2. Souvenir._handle_outgoing():
   a. Read messages_raw from envelope.context["atelier"]["messages_raw"]
      (full LangChain message list, serialized by Atelier)
   b. RPUSH relais:context:{session_id} [messages_raw blob]
      LTRIM -20, EXPIRE 24h (one JSON blob per turn, flattened on read)
   c. long_term_store.archive(envelope)     ← SQLite upsert on correlation_id
      (fields: user_content, assistant_content, messages_raw JSON)
```

### Compaction contexte

Planifié. Aucun mécanisme de compaction n'est branché dans `souvenir/main.py` aujourd'hui.

### Scopes mémoire

Concepts de design non implémentés dans le pipeline actuel. Le comportement réel se limite à :
- contexte court terme par `session_id`
- archivage SQLite par session et sender
- fichiers mémoire persistants isolés par `user_id`

### Pagination native

`LongTermStore` expose des méthodes de requête utilitaires internes, dont une requête paginée. Aucune API admin ou interface utilisateur branchée n'exploite encore cette pagination dans le pipeline actuel.

---

## 13. L'Archiviste — pure observer avec pipeline observation

L'Archiviste est un observer pur — il consomme sans jamais publier.

**Deux consumer groups en parallèle :**

1. `archiviste_group` — observe `relais:logs`, `relais:events:system`, `relais:events:messages`
2. `archiviste_pipeline_group` — observe tous les streams du pipeline pour visibilité end-to-end

**Streams observés :**
- `relais:messages:incoming`
- `relais:security`
- `relais:tasks`
- `relais:tasks:failed` (DLQ)
- `relais:messages:outgoing:discord`

**Événements observés en plus :**
- `relais:logs`
- `relais:events:system`
- `relais:events:messages`

**Enrichissement des logs :** Chaque entrée dans `relais:logs` est enrichie par les briques avec `correlation_id`, `sender_id`, et `content_preview` (60 premiers caractères). L'Archiviste préfixe les lignes de log avec `[{cid[:8]}] {sender_id} | message`.

**Rétention :** non implémentée dans L'Archiviste actuel. Toute logique de cleanup pilotée par Le Veilleur reste planifiée.

---

## 24. Le Commandant — commandes globales hors-LLM

### Rôle

Le Commandant exécute les commandes slash autorisées hors-LLM. Il reçoit uniquement les enveloppes pré-filtrées par La Sentinelle : commandes connues **et** autorisées pour le rôle de l'utilisateur. Il exécute l'action demandée immédiatement et répond directement à l'utilisateur sur le canal d'origine. Aucun token LLM n'est consommé.

**Taxonomie :** Transformer — consomme `relais:commands` et `relais:commandant:query`, publie vers `relais:messages:outgoing:{channel}`, `relais:memory:request` et `relais:commandant:catalog:{correlation_id}`.

### Architecture

```
relais:security
  ▼
SENTINELLE (entrant)
  ├─► [message normal]          → relais:tasks         (→ Atelier)
  ├─► [commande connue + ACL OK] → relais:commands     (→ Commandant)
  ├─► [commande inconnue]       → réponse inline "Commande inconnue : /xxx"
  └─► [commande non autorisée]  → réponse inline "Vous n'avez pas la permission..."
```

Le Commandant consomme deux streams (`ack_mode="always"` sur les deux) :
- `relais:commands` (`commandant_group`) — dispatch des slash commandes. Toutes les enveloppes arrivant ici ont déjà passé l'ACL identité et l'ACL commande.
- `relais:commandant:query` (`commandant_catalog_group`) — côté lecture CQRS : le REST adapter publie une enveloppe `action=catalog.query` ; Le Commandant répond par un `LPUSH` sur `relais:commandant:catalog:{correlation_id}` (TTL 7 s) que le REST adapter récupère via `BRPOP` (timeout 5 s).

### Commandes supportées

| Commande | Description | Portée |
|---|---|---|
| `/clear` | Efface l'historique de la session courante | Session courante |
| `/help` | Affiche la liste des commandes disponibles | Session courante |

### Comportement détaillé

#### `/clear`

1. Le Commandant publie `{"action": "clear", "session_id": <session_id>, "envelope_json": <envelope serialisée>}` sur `relais:memory:request`
2. Le Souvenir efface :
   - Le contexte Redis court terme (`relais:context:{session_id}`)
   - Les messages SQLite de la session (`archived_messages`)
3. Le Souvenir répond sur `relais:messages:outgoing:{channel}` : `"✓ Historique de conversation effacé."` (confirmation envoyée par `ClearHandler` après le vrai nettoyage, si `envelope_json` est présent dans la requête)

#### `/help`

1. Le Commandant parcourt `COMMAND_REGISTRY`
2. Il construit une réponse texte listant chaque commande et sa description
3. Il publie cette réponse sur `relais:messages:outgoing:{channel}`

### Accès Redis requis

| Opération | Stream / Clé |
|---|---|
| XREADGROUP (lecture) | `relais:commands` |
| XREADGROUP (lecture) | `relais:commandant:query` |
| XADD (réponse canal) | `relais:messages:outgoing:*` |
| XADD (effacement mémoire) | `relais:memory:request` |
| LPUSH + EXPIRE (catalogue) | `relais:commandant:catalog:{correlation_id}` |

### Extensibilité

Toute nouvelle commande hors-LLM s'ajoute dans Le Commandant sans toucher aux autres briques. La source de vérité est `COMMAND_REGISTRY` dans `commandant/commands.py`; `common.command_utils` en dérive automatiquement `KNOWN_COMMANDS` pour la reconnaissance côté Sentinelle.

La détection et le rejet des commandes inconnues ou non autorisées sont entièrement gérés par La Sentinelle — le Commandant ne reçoit que des commandes valides et autorisées.

---

## 14. Corrélation end-to-end

```
Généré une seule fois par L'Aiguilleur.
Propagé via Envelope.from_parent() dans TOUTES les enveloppes dérivées.
Jamais régénéré.
Inclus dans tous les events Redis et tous les logs.
Le Scrutateur expose GET /trace/{correlation_id}.
```

---

## 15. SOUL.md & prompts — personnalité JARVIS multi-couches

### Structure multi-couches des prompts

L'Atelier assemble le system prompt en 4 couches, séparées par `\n\n---\n\n` :

```
prompts/
├── soul/
│   └── SOUL.md                    ← Layer 1: Core personality (always attempted)
├── roles/
│   ├── admin.md                   ← Layer 2: Role overlay (`roles/{role}.md`)
│   └── user.md
├── users/                         ← Layer 3: Per-user override (chemin explicite relatif via `prompt_path`)
│   └── discord_12345_678.md
└── channels/
    ├── discord_default.md         ← Layer 4: Channel formatting rules
    ├── telegram_default.md
    └── whatsapp_default.md
```

**Ordre d'assemblage :**

| Ordre | Source | Fichier | Toujours présent |
|-------|--------|------|---|
| 1 | Personality | `soul/SOUL.md` | Oui (chargement tenté systématiquement) |
| 2 | Role | `prompts/roles/{role}.md` | Non (optionnel) |
| 3 | User | chemin explicite relatif `prompt_path` | Non (optionnel, warning si fichier absent) |
| 4 | Channel | `prompts/channels/{channel}_default.md` | Non (warning si absent) |

> **Note as-built :** `SoulAssembler` ignore silencieusement les fichiers manquants ou vides. Les couches de policy ou de user facts ne sont pas assemblées aujourd'hui.

### Internationalisation — SOUL.md gère tout

SOUL.md instruit JARVIS d'utiliser la langue de son interlocuteur. Le LLM détecte automatiquement la langue entrante et répond dans la même langue. Les notifications système natives restent en français car elles sont générées par Le Crieur, pas par le LLM.

### Construction du prompt final

Dans l'état actuel du code, le prompt système est uniquement la concaténation des couches `SOUL`, `role`, `user`, `channel`. Le contexte conversationnel est injecté séparément au moment de construire la liste des messages envoyés à l'agent.

---

## 16. Profils — config/atelier/profiles.yaml

La structure réelle parsée par `common/profile_loader.py` est volontairement compacte. Les politiques d'accès aux skills et outils MCP sont portées par `portail.yaml` via `UserRecord`, pas par `atelier/profiles.yaml`.

> **Note :** `atelier/profile_loader.py` est désormais un shim de rétrocompatibilité qui ré-exporte depuis `common/profile_loader.py`. Importer directement depuis `common.profile_loader` dans le nouveau code.

### Règle de résolution du profil

Le profil actif pour un message entrant est résolu dans cet ordre strict (le premier trouvé gagne) :

1. **`aiguilleur.yaml:profile`** — profil défini sur le canal d'origine (stampé par l'Aiguilleur dans `envelope.context["aiguilleur"]["channel_profile"]`)
2. **`"default"`** — valeur de repli ultime

> La résolution est effectuée par le **Portail** et le résultat est stampé dans `envelope.context["portail"]["llm_profile"]` (clé dans le namespace portail). L'Atelier relit ensuite cette valeur depuis `envelope.context["portail"]["llm_profile"]`.
>
> Note : les champs `llm_profile` au niveau utilisateur (`portail.yaml:users.<id>.llm_profile`) et rôle (`roles.<role>.llm_profile`) ont été supprimés — la priorité se fait désormais uniquement via le canal ou la valeur par défaut système.

```yaml
profiles:
  default:
    model: anthropic:claude-sonnet-4-6
    temperature: 0.7
    max_tokens: 2048
    max_turns: 20
    base_url: null
    api_key_env: ANTHROPIC_API_KEY
    fallback_model: null
    shell_timeout_seconds: 30
    max_turn_seconds: 300
    resilience:
      retry_attempts: 3
      retry_delays: [2, 5, 15]
      fallback_model: null
```

**Champs effectivement parsés :**
- `model`
- `temperature`
- `max_tokens`
- `max_turns`
- `base_url`
- `api_key_env`
- `fallback_model`
- `shell_timeout_seconds` (défaut 30) — timeout wall-clock par appel shell (`_HtmlSafeShellBackend.execute`)
- `max_turn_seconds` (défaut 300) — timeout wall-clock pour le tour agentique complet (`AgentExecutor.execute`) ; 0 = désactivé
- `resilience.retry_attempts`
- `resilience.retry_delays`
- `resilience.fallback_model`

---

## 17. Politique de réponse automatique

Non implémentée dans le pipeline actuel. Toute logique de reply policy, vacation, in_meeting ou debounce relève encore du design cible et doit être considérée comme planifiée.

---

## 18. Gestion des médias

### Principe

Le type `MediaRef` existe dans `common/envelope.py` et peut être sérialisé dans une `Envelope`.

En revanche, le pipeline actuel ne branche pas encore d'ingestion média complète côté adaptateurs, ni de stockage TTL effectif, ni d'injection média dans le prompt.

**Champ `media_refs` de l'Envelope :**

| Champ | Type | Description |
|-------|------|-------------|
| `media_id` | str | UUID unique |
| `path` | str | Chemin local dans `RELAIS_HOME/media/` |
| `mime_type` | str | Type MIME |
| `size_bytes` | int | Taille en octets |
| `expires_in_hours` | int | TTL (24h) |

### Nettoyage

Planifié. Aucun nettoyage média opérationnel n'est branché dans les adaptateurs actuels.

---

## 19. Système d'extensions

```
INTERCEPTEUR (tisserand/)  Planifié
OBSERVER (relais:events:*) Partiellement préparé côté Archiviste
```

**Règle de décision fondamentale :**
```
"Peut bloquer ?"  → intercepteur (Le Tisserand)
"Observe ?"       → Redis event  (out-of-process)
```

### Intercepteurs natifs

Le framework d'intercepteurs n'est pas implémenté dans le repo courant. Cette section documente donc une direction d'architecture future, pas une capacité active.

---

## 20. Sécurité

### .env — secrets et intégrations

```bash
# LLM providers
ANTHROPIC_API_KEY=sk-ant-...
OPENROUTER_API_KEY=sk-or-...

# Messaging bridges
DISCORD_BOT_TOKEN=...

# Canaux / intégrations planifiés
TELEGRAM_BOT_TOKEN=...
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
REST_API_KEY=...

# Redis
REDIS_PASSWORD=...
REDIS_PASS_AIGUILLEUR=...
REDIS_PASS_PORTAIL=...
REDIS_PASS_SENTINELLE=...
REDIS_PASS_ATELIER=...
REDIS_PASS_SOUVENIR=...
REDIS_PASS_ARCHIVISTE=...
# Les autres credentials de briques planifiées sont omis ici

# MCP servers
GITLAB_TOKEN=...
GITLAB_URL=https://gitlab.company.com
BRAVE_API_KEY=...
GOOGLE_CREDENTIALS_PATH=<INSTALL_CONFIG_DIR>/config/google_credentials.json

# Variables supplémentaires selon les MCP/configs actifs
```

### Graceful shutdown

Chaque brique implémente `GracefulShutdown` : handlers SIGTERM/SIGINT, tracking des tâches async en vol, attente de fin avant exit. Le `stopwaitsecs` supervisord doit être supérieur au timeout Python de chaque brique.

---

## 21. Structure du projet

```
<repo>/
│                                         Ne contient PAS de données utilisateur
├── .env.example
├── .env                               ← JAMAIS committé
├── .gitignore
├── supervisord.conf
├── pyproject.toml
├── README.md
│
├── config/                            ← Templates système (*.default)
│   ├── config.yaml.default
│   ├── atelier.yaml.default           ← config comportementale Atelier (progress events)
│   ├── portail.yaml.default
│   ├── sentinelle.yaml.default
│   ├── aiguilleur.yaml.default
│   ├── redis.conf
│   ├── HEARTBEAT.md.default
│   └── atelier/
│       ├── profiles.yaml.default
│       └── mcp_servers.yaml.default
│
├── prompts/                           ← Prompts système par défaut
│   ├── soul/
│   │   ├── SOUL.md.default
│   │   └── variants/
│   │       ├── SOUL_concise.md.default
│   │       └── SOUL_professional.md.default
│   ├── channels/
│   │   ├── telegram_default.md
│   │   └── whatsapp_default.md
│   ├── policies/
│   │   ├── out_of_hours.md
│   │   ├── vacation.md
│   │   └── in_meeting.md
│   ├── roles/
│   └── users/
│
├── common/
│   ├── envelope.py                    ← Envelope + PushEnvelope + MediaRef
│   ├── redis_client.py
│   ├── shutdown.py
│   ├── config_loader.py               ← get_relais_home() + resolve_config_path()
│   ├── init.py                        ← initialize_user_dir()
│   ├── user_record.py                 ← UserRecord
│   ├── profile_loader.py              ← ProfileConfig + ResilienceConfig (common — partagé)
│   └── markdown_converter.py
│
├── aiguilleur/
│   ├── main.py                        ← AiguilleurManager (processus unifié)
│   ├── base.py                        ← AiguilleurBase ABC
│   ├── channel_config.py
│   └── channels/
│       └── discord/adapter.py
│
├── portail/
│   ├── main.py
│   └── user_registry.py
│
├── sentinelle/
│   ├── main.py
│   └── acl.py                         ← ACLManager context-aware (allowlist/blocklist)
│
├── atelier/
│   ├── main.py
│   ├── agent_executor.py
│   ├── mcp_adapter.py
│   ├── mcp_session_manager.py
│   ├── tool_policy.py
│   ├── mcp_loader.py
│   ├── soul_assembler.py
│   ├── stream_publisher.py
│   ├── souvenir_backend.py
│   ├── profile_loader.py              ← shim de rétrocompat → re-export depuis common/profile_loader.py
│   └── progress_config.py             ← ProgressConfig (master switch + per-event flags)
│
├── souvenir/
│   ├── main.py
│   ├── file_store.py
│   ├── long_term_store.py
│   ├── models.py
│   ├── handlers/
│   └── migrations/
│
├── archiviste/
│   └── main.py
│
└── tests/
    └── *.py

─────────────────────────────────────────────────────────────────────

<RELAIS_HOME>/                        ← Répertoire utilisateur (données & config)
│                                         Résolu par get_relais_home()
│                                         Override via RELAIS_HOME=...
│                                         Défaut actuel du code : `<repo>/.relais`
│                                         Créé automatiquement au 1er lancement
├── config/
│   ├── config.yaml
│   ├── atelier.yaml
│   ├── portail.yaml
│   ├── sentinelle.yaml
│   ├── aiguilleur.yaml
│   ├── HEARTBEAT.md
│   └── atelier/
│       ├── profiles.yaml
│       └── mcp_servers.yaml
│
├── prompts/
│   ├── soul/
│   ├── channels/
│   ├── policies/
│   ├── roles/
│   └── users/
│
├── skills/
│   ├── CLAUDE.md                      ← registre skills actifs
│
├── media/                             ← TTL 24h
│
├── logs/
│   └── events.jsonl
│
├── storage/
│   └── memory.db
│
└── backup/
```

---

## 22. La Charte RELAIS

```
┌─────────────────────────────────────────────────────────────────┐
│                      LA CHARTE RELAIS                           │
├─────────────────────────────────────────────────────────────────┤
│  ARCHITECTURE                                                    │
│  1.  UNE BRIQUE = UNE RESPONSABILITÉ                            │
│  2.  CODE EN ANGLAIS — noms de briques en français              │
│  3.  ZÉRO IMPORT CROISÉ — common/ uniquement partagé           │
│  4.  MOINS DE 300 LIGNES par fichier main.py                    │
│                                                                  │
│  RÉPERTOIRE UTILISATEUR                                         │
│  5.  RELAIS_HOME = config + skills + logs + médias              │
│      <INSTALL_CONFIG_DIR>/ = code système uniquement            │
│      Cascade : RELAIS_HOME > <INSTALL_CONFIG_DIR> > ./          │
│      Override via RELAIS_HOME=... si nécessaire                 │
│      Défaut actuel du code : <repo>/.relais                     │
│                                                                  │
│  INFRASTRUCTURE                                                  │
│  6.  REDIS = Unix socket — port TCP = 0 — ACL par brique       │
│  7.  SUPERVISORD gère les processus — Le Vigile via XML-RPC     │
│  8.  MCP globaux dans supervisord — contextuels via L'Atelier   │
│  9.  SECRETS dans .env — jamais dans config.yaml               │
│  10. GRACEFUL SHUTDOWN — SIGTERM → finit les tâches in-flight  │
│                                                                  │
│  COMMUNICATION                                                   │
│  11. STREAMS pour topics critiques — PUB/SUB pour monitoring   │
│  12. relais:logs → Stream (audit ne se perd jamais)            │
│  13. PUSH high/critical → tous canaux actifs (intentionnel)    │
│  14. WEBHOOKS via Aiguilleur/rest — HMAC avant publication     │
│  15. CORRELATION_ID via Envelope.from_parent() — une seule fois│
│  16. HOT RELOAD via relais:admin:reload — pas de redémarrage   │
│                                                                  │
│  AGENTS & PROFILS                                               │
│  17. 3 RÔLES HUMAINS : ADMIN · SUPERVISEUR · USER              │
│  18. usr_system dans portail.yaml — usage planifié             │
│  19. UN PROFIL = modèle + paramètres provider + résilience     │
│  20. LLM RESILIENCE : retries/DLQ selon ProfileConfig          │
│  21. SOUL + role + user + channel = prompt système             │
│  22. Le contexte conversationnel est injecté hors prompt       │
│  23. Compaction et sous-agents restent planifiés               │
│  24. Les accès skills/MCP sont portés par UserRecord           │
│                                                                  │
│  MÉDIAS & DONNÉES                                               │
│  25. MediaRef est disponible, pipeline média complet planifié  │
│  26. Les skills sont chargés depuis RELAIS_HOME/skills         │
│  27. Backup/Rétention restent des briques futures              │
│  28. LongTermStore expose des requêtes utilitaires internes    │
│  29. Pagination disponible côté store, pas côté interface      │
│  30. Conversion Markdown multi-canal reste partiellement future│
│                                                                  │
│  EXTENSIBILITÉ                                                   │
│  31. Le Tisserand reste planifié                                │
│  32. L'Archiviste observe déjà logs + pipeline + events        │
│  33. Les autres observers/outils d'admin restent planifiés     │
│                                                                  │
│  DÉCISION FONDAMENTALE                                          │
│  34. "PEUT BLOQUER ?" → Le Tisserand                           │
│      "OBSERVE ?"      → Redis events                           │
│      "PERSISTE ?"     → Le Souvenir                            │
│      "ROUTE ?"        → Le Portail                             │
│      "SÉCURISE ?"     → La Sentinelle                          │
│      "PLANIFIE ?"     → Le Veilleur (publish)                  │
│      "ANALYSE BATCH?"→ Le Forgeron (lit SQLite, exit)          │
│      "LOG ?"          → L'Archiviste (observer Stream)         │
│      "MONITORE ?"     → Le Scrutateur (observer Pub/Sub)       │
│      "OÙ EST LA DATA?"→ RELAIS_HOME (défaut : <repo>/.relais)  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 23. Planifié — Briques futures

Les briques de cette section sont entièrement spécifiées fonctionnellement mais pas encore implémentées.

---

### 🌙 Le Veilleur — planification, backup, rétention

**Taxonomie :** Pure Publisher — pas de LLM, pas d'exécution directe d'agents.

**Rôle :** Publie des `AgentTask` dans `relais:tasks` selon les tâches définies dans `HEARTBEAT.md`. L'Atelier les exécute avec le profil `SCHEDULER_AGENT` sous l'identité `usr_system`.

**HEARTBEAT.md — tâches configurées :**

```yaml
# config/HEARTBEAT.md

## MR GitLab check
- Cadence: every 15 minutes
- Prompt: Scanne les nouvelles MR assignées à Benjamin. Signale uniquement les nouvelles.

## Email check
- Cadence: every 30 minutes (08:00-21:00 only)
- Prompt: Vérifie les emails urgents.

## Calendar check
- Cadence: every 2 hours (08:00-22:00 only)
- Prompt: Événements dans les 24h à venir.

## System health
- Cadence: daily at 03:00
- Prompt: Vérification disque, mémoire.

## Media cleanup
- Cadence: daily at 03:00
- Prompt: SYSTEM:cleanup_media

## Log retention cleanup
- Cadence: daily at 03:30
- Prompt: SYSTEM:cleanup_logs

## Backup
- Cadence: daily at 04:00 (if backup.enabled in config)
- Prompt: SYSTEM:backup

## Auto-forgeron run
- Cadence: daily at 02:00
- Prompt: SYSTEM:start_forgeron
```

**Backup — configurable :**

```yaml
# config/config.yaml
backup:
  enabled: true
  path: "/Volumes/Backup/relais"
  files:
    - souvenir/relais_memory.db
    - archiviste/logs/relais.db
    - skills/
    - soul/
    - config/
  sqlite_backup_api: true   # utilise .backup() SQLite (safe en concurrent)
  rsync_options: "-av --delete"
```

**Rétention :**

```yaml
# config/config.yaml
retention:
  jsonl_days: 90          # fichiers JSONL
  sqlite_days: 365        # enregistrements SQLite L'Archiviste
  audit_days: null        # logs d'audit — jamais supprimés
  media_hours: 24         # fichiers médias temporaires
```

---

### 🔧 Le Forgeron — auto-apprentissage & versioning skills

**Taxonomie :** BrickBase long-running — deux boucles consommateurs Redis ; `autostart=true`, `autorestart=true`.

**Rôle :** Améliore les skills existants via édition directe (`SkillEditor`) **et** crée automatiquement de nouveaux skills à partir des patterns récurrents dans les sessions (auto-creation pipeline). Publie des notifications utilisateur quand un skill est créé ou modifié.

**Pipeline édition directe :**

Consomme `relais:skill:trace` (publié par Atelier après chaque tour). `SkillTraceStore` (SQLite) accumule une ligne par tour. `SkillEditor` (LLM précis, profil `edit_profile`, défaut `"precise"`) reçoit le SKILL.md courant + la trace de conversation scopée au skill cible, appelle le LLM une seule fois avec `with_structured_output`, et réécrit le SKILL.md directement si `changed=True`. Déclenché par erreurs d'outils (`tool_error_count >= edit_min_tool_errors`), tour avorté (DLQ, `tool_error_count == -1`), succès après échec, ou seuil d'appels cumulés (`edit_call_threshold`). Cooldown Redis par skill (`edit_cooldown_seconds`) empêche les éditions trop fréquentes. Pour les skills provenant d'un bundle, `skill_paths` dans la trace indique le chemin absolu du répertoire.

**Pipeline auto-création (session archives) :**

Consomme `relais:memory:request` via groupe dédié `forgeron_archive_group` (indépendant du groupe de Souvenir). `IntentLabeler` (Haiku LLM, structured output) extrait un label d'intention normalisé (snake_case) de chaque session. Quand N sessions partagent le même label, `SkillCreator` (LLM précis, structured output) génère un nouveau `SKILL.md` automatiquement.

**Classes clés :**

| Classe | Rôle |
|--------|------|
| `Forgeron` | BrickBase — deux boucles : `relais:skill:trace` + `relais:memory:request` |
| `SkillTraceStore` | SQLite — accumule les traces par skill (tool_call_count, tool_error_count) |
| `SkillEditor` | LLM précis — réécrit SKILL.md directement depuis trace scopée (import paresseux) |
| `SessionStore` | SQLite — patterns d'intention par session (pipeline auto-création) |
| `IntentLabeler` | Haiku LLM — extrait label snake_case depuis session (import paresseux) |
| `SkillCreator` | LLM précis — génère SKILL.md depuis exemples (import paresseux) |

**Redis channels :**

```
Consommés :
  relais:skill:trace         (consumer group: forgeron_group)
  relais:memory:request      (consumer group: forgeron_archive_group)

Produits :
  relais:events:system       — skill_created
  relais:messages:outgoing_pending — notifications utilisateur
  relais:logs                — logs opérationnels

Redis keys (cooldowns) :
  relais:skill:edit_cooldown:{skill_name}           — cooldown édition directe
  relais:skill:creation_cooldown:{intent_label}     — auto-creation cooldown

XACK : ack_mode="always" sur les deux streams (consumer advisory — perte acceptable).
```

**Note :** L'amélioration des skills a évolué vers une édition directe (`SkillEditor`) en remplacement de l'approche 2-phases (changelog + consolidation périodique). Atelier publie les traces sur `relais:skill:trace` ; Forgeron décide du déclenchement et réécrit le SKILL.md en un seul appel LLM.

---

### 📣 Le Crieur — push proactif & multi-canal

**Taxonomie :** Transformer — consomme `relais:push:{urgency}`, résout les destinataires, publie vers `relais:notifications:{role}`.

**Rôle :** Envoie des notifications proactives sur les canaux actifs de l'utilisateur. La stratégie de routage est intentionnellement multi-canal pour `high` et `critical`.

```yaml
# config/config.yaml
crieur:
  routing_strategy:
    normal:   last_active   # 1 canal — évite le bruit quotidien
    high:     all_active    # tous les canaux actifs — intentionnel
    critical: all_active    # tous les canaux + notification OS native
```

**Résolution des destinataires :** par `target_user_id`, `target_role`, ou `session_id`. Si aucun canal actif, fallback sur le `preferred_channel` de l'utilisateur.

---

### 🔱 Le Vigile — administration NLP & hot reload

**Taxonomie :** Admin — pilote supervisord via XML-RPC + publie `relais:admin:reload`.

**Rôle :** Interprète les commandes admin en langage naturel et les traduit en actions système.

**Exemples de commandes :**
```
"status de toutes les briques"
"redémarre le relay WhatsApp"
"recharge la config"
"active le mode réunion pour 1h"
"combien de tokens consommés aujourd'hui ?"
"liste les extensions actives"
"utilise la version du 15 mars pour le skill mr_review"
"montre les messages en attente de debounce"
"déclenche un backup maintenant"
```

**Hot config reload :** Le Vigile publie `relais:admin:reload` avec la liste des fichiers à recharger. Chaque brique souscrit à ce topic et recharge sa section de config en mémoire sans redémarrage.

---

### 📊 Le Tableau — TUI bidirectionnel

**Taxonomie :** Admin + Relay — interface locale Textual, `autostart=false`.

**Rôle :** Interface TUI 3 colonnes avec streaming token-par-token, notifications push async, et monitoring supervisord en temps réel. Les messages push arrivent sans interrompre la saisie.

```
💬 Messages utilisateur   → fond bleu
🤖 Réponses JARVIS        → streaming token par token
📣 Notifications push     → fond ambre
⚙️ Événements système     → fond gris
```

---

### 🧵 Le Tisserand — extensions intercepteurs

**Taxonomie :** Interceptor Chain — in-process dans L'Atelier.

**Rôle :** Chaîne d'intercepteurs Python exécutés in-process avant/pendant l'exécution agentique. Timeout 2s par intercepteur. Exception → skippé + loggé. RELAIS ne crashe jamais à cause d'une extension.

**Interface développeur :**

| Hook | Rôle |
|------|------|
| `on_request` | Avant envoi au LLM |
| `on_stream_chunk` | Sur chaque token streamé |
| `on_tool_call_start` | Avant exécution d'un outil |
| `on_command` | Sur commandes admin |

`return None` = blocage du message. `return event` = passage à l'intercepteur suivant.

---

### 🔍 Le Scrutateur — monitoring pure observer

**Taxonomie :** Pure Observer — consomme `relais:events:*` (Pub/Sub), expose métriques Prometheus.

**Endpoints :**
```
GET /metrics                → Prometheus
GET /health                 → statut global
GET /stats                  → résumé (sessions actives, coûts)
GET /trace/{correlation_id} → chemin complet d'une requête
```

**Métriques exposées :** `relais_requests_total`, `relais_tokens_total`, `relais_request_duration_seconds`, `relais_tool_calls_total`, `relais_errors_total`, `relais_active_sessions`, `relais_daily_cost_usd`, `relais_interceptor_blocks_total`.

Dashboards Grafana dans `scrutateur/grafana/dashboards/`.
