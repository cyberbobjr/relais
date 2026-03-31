# RELAIS — Document d'Architecture

> **RELAIS** — *Station de relais : reçoit des messages de toutes origines,*
> *les achemine vers leur destination avec fiabilité et continuité.*
>
> Framework d'agents conversationnels multi-canaux, autonomes, extensibles,
> et auto-apprenants. Projet francophone, code anglais.

---

## Table des matières

1. Vision & Objectifs
2. Répertoire utilisateur — ~/.relais/
3. Convention de nommage
4. Taxonomie des briques
5. Les Briques — tableau d'ensemble
6. Infrastructure — supervisord & MCP servers
7. Le Coursier — Redis sécurisé
8. L'Aiguilleur — adaptateur de canaux & formatage Markdown
9. Le Portail — routage & politique de réponse
10. La Sentinelle — sécurité & profils
11. L'Atelier — exécution des agents, résilience LLM, outils internes
12. Le Souvenir — mémoire, compaction, pagination
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

## 2. Répertoire utilisateur — ~/.relais/

### Convention

Toute la configuration personnalisée, les skills, les logs et les médias sont stockés dans le répertoire du **compte qui lance RELAIS**. Ce pattern suit la convention Unix/XDG — les applications daemon ne polluent pas l'installation système avec des données utilisateur.

```
~/.relais/                        ← RELAIS_HOME (défaut)
│
├── config/
│   ├── config.yaml               ← surcharge /opt/relais/config/config.yaml
│   ├── profiles.yaml             ← profils personnalisés
│   ├── users.yaml                ← registry utilisateurs
│   ├── reply_policy.yaml         ← politique de réponse
│   ├── mcp_servers.yaml          ← MCP servers additionnels
│   └── HEARTBEAT.md              ← tâches planifiées personnalisées
│
├── soul/
│   ├── SOUL.md                   ← personnalité JARVIS personnalisée
│   └── variants/
│       ├── SOUL_concise.md
│       └── SOUL_professional.md
│
├── prompts/                      ← prompts de tâche personnalisés
│   ├── marie.md
│   └── family.md
│
├── skills/
│   ├── manual/                   ← skills écrits à la main par l'utilisateur
│   │   └── SKILL_my_custom.md
│   └── auto/                     ← skills auto-générés par Le Forgeron
│       └── SKILL_auto_mr_review_20260327.md
│
├── media/                        ← fichiers médias temporaires (TTL 24h)
│
├── logs/                         ← L'Archiviste écrit ici
│   ├── relais.db                 ← SQLite L'Archiviste
│   └── YYYY-MM-DD.jsonl          ← JSONL rotatifs
│
└── backup/                       ← backups locaux (si backup.path non configuré)
```

### Cascade de résolution

```
1. ~/.relais/config/     ← config utilisateur (priorité maximale)
2. /opt/relais/config/   ← installation système
3. ./config/             ← répertoire courant (mode dev)
```

La variable `RELAIS_HOME` permet de surcharger `~/.relais/` explicitement (ex: `/srv/relais` en production, `/tmp/relais-test` pour les tests d'intégration).

### Initialisation au premier lancement

Au démarrage, RELAIS crée automatiquement `~/.relais/` et y copie les fichiers `.default` depuis `/opt/relais/`. Les fichiers déjà présents ne sont jamais écrasés — l'opération est idempotente.

### Impact par brique

| Brique | Ce qui change |
|---|---|
| L'Archiviste | Écrit dans `~/.relais/logs/` |
| Le Forgeron | Lit/écrit dans `~/.relais/skills/auto/` |
| L'Atelier | Charge les skills depuis `~/.relais/skills/` |
| Le Souvenir | DB dans `~/.relais/storage/memory.db` |
| Le Portail | Charge `~/.relais/config/reply_policy.yaml` |
| Le Vigile | Charge `~/.relais/soul/SOUL.md` pour hot reload |
| Le Veilleur | Lit `~/.relais/config/HEARTBEAT.md` + backup vers `~/.relais/backup/` |
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
| 🚦 **L'Aiguilleur** | `aiguilleur/` | Relay | Adaptateur de canaux — processus unifié | ← `outgoing:{ch}` / → `incoming:{ch}` |
| 🏛️ **Le Portail** | `portail/` | Transformer | Routage, identification, politique | ← `incoming:{ch}` / → `security` |
| 🛡️ **La Sentinelle** | `sentinelle/` | Transformer | ACL, profils, guardrails | ← `security` / → `tasks` |
| 📨 **Le Coursier** | Redis | Infrastructure | Bus messages Unix socket | — |
| ⚒️ **L'Atelier** | `atelier/` | Stream Consumer | Exécution agents LLM | ← `tasks` / → `outgoing:{ch}`, `tasks:failed` |
| 💭 **Le Souvenir** | `souvenir/` | Stream Consumer | Mémoire contexte + longue durée | ← `memory:request` + `outgoing:*` / → `memory:response` |
| 📚 **L'Archiviste** | `archiviste/` | Pure Observer | Logs → JSONL + SQLite | ← `logs` + `events:*` (pubsub) |

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
| 🎮 **Le Commandant** | `commandant/` | Transformer | Commandes globales hors-LLM (`/clear`, `/dnd`, `/brb`) |

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

Toutes les briques loggent dans `~/.relais/logs/` via `stdout_logfile` supervisord.

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

### config/mcp_servers.yaml

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
> **Timeout et nombre max d'outils** — configurés par profil dans `profiles.yaml` (`mcp_timeout`, `mcp_max_tools`), pas dans ce fichier.

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
  ~relais:security
  +subscribe +publish +xreadgroup +xack +xadd

user atelier    on >${REDIS_PASS_ATELIER}
  ~relais:tasks ~relais:tasks:failed ~relais:memory:* ~relais:messages:streaming:* ~relais:messages:outgoing:* ~relais:events:* ~relais:logs
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
  relais:messages:incoming:{channel}    Aiguilleur → Portail
  relais:messages:outgoing:{channel}    Atelier → Aiguilleur + Souvenir (observer)
  relais:messages:streaming:{ch}:{corr} Atelier → Aiguilleur (progressive chunks)
  relais:security                       Portail ↔ Sentinelle
  relais:logs                           Toutes → Archiviste (audit critique)
  relais:skills:new                     Forgeron → Vigile

LISTS (fast cache — volatile)
  relais:context:{session_id}           Souvenir stores (RPUSH/LTRIM), Atelier reads

PUB/SUB (fire & forget — perte acceptable)
  relais:messages:incoming       Aiguilleur → Portail
  relais:messages:outgoing:{ch}  → Aiguilleur cible
  relais:push:{urgency}          Toutes → Crieur
  relais:notifications:{role}    Crieur → Aiguilleurs
  relais:active_sessions:*       Portail → (Crieur lit)
  relais:events:*                Monitoring — Scrutateur
  relais:admin:*                 Vigile ↔ Briques
  relais:admin:reload            Vigile → Briques (hot reload)
  relais:webhooks:*              Aiguilleur/rest → Crieur/Atelier
  relais:media:*                 Aiguilleur → Portail (métadonnées médias)
```

---

## 8. L'Aiguilleur — adaptateur de canaux & formatage Markdown

### Architecture — processus unifié

L'Aiguilleur est un **processus unique** (`aiguilleur/main.py`) qui gère tous les adaptateurs de canaux. L'`AiguilleurManager` charge les canaux depuis `channels.yaml` et instancie les adaptateurs au démarrage.

- **Adaptateurs natifs** (`type: native`) — thread Python + `asyncio.run`, ex: `DiscordAiguilleur`
- **Adaptateurs externes** (`type: external`) — `subprocess.Popen`, pour les adaptateurs non-Python
- **Restart automatique** — backoff exponentiel `min(2^restart_count, 30)` secondes, `max_restarts` configurable
- **Découverte automatique** — convention `aiguilleur.channels.{name}.adapter.{Name}Aiguilleur`, surchargeable via `class_path`

### Deux responsabilités à la sortie

L'Aiguilleur fait la conversion Markdown à la **sortie uniquement** (réponses vers le canal). Chaque adaptateur connaît les règles syntaxiques de son canal :

| Canal | Conversion |
|---|---|
| Discord | Passthrough (Markdown standard natif) |
| Telegram | Markdown → MarkdownV2 |
| Slack | Markdown → mrkdwn |
| WhatsApp | Strip (pas de Markdown) |
| Signal | Strip |
| REST | Passthrough (brut, client gère le rendu) |
| TUI | Passthrough (Textual rend le Markdown) |

### Configuration des canaux via `channels.yaml`

```yaml
channels:
  discord:
    enabled: true                    # Activé/désactivé
    streaming: true                  # Streaming progressif support
    type: native                     # "native" (Python, thread+asyncio) | "external" (subprocess)
    class_path: null                 # Override : "aiguilleur.channels.discord.adapter.DiscordAiguilleur"
    max_restarts: 5

  telegram:
    enabled: false
    streaming: true
    type: native
    class_path: null
    max_restarts: 5

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

  # Exemples externes (non-Python)
  whatsapp:
    enabled: false
    streaming: false
    type: external
    command: "node"
    args: ["aiguilleur/whatsapp/index.js"]
    max_restarts: 3
```

**Paramètres clés :**
- `enabled` — toggle sans suppression de code
- `streaming` — flag utilisé par L'Atelier pour `STREAMING_CAPABLE_CHANNELS`
- `type` — `native` (thread Python + asyncio) ou `external` (subprocess)
- `class_path` — override de la classe adaptateur
- `max_restarts` — max avant abandon, restart avec backoff exponentiel
- `command`/`args` — requis pour `type: external` uniquement

### Tableau des canaux

| Canal | Lib | Markdown | Auto-start | Auth |
|---|---|---|---|---|
| Telegram | python-telegram-bot ≥ 21 | MarkdownV2 | Oui | Bot token |
| Discord | discord.py ≥ 2.4 | Standard | Oui | Bot token |
| Slack | slack-bolt Python | mrkdwn | Non | OAuth |
| Matrix | matrix-nio ≥ 0.24 | HTML/MD | Non | Homeserver |
| Teams | botbuilder-python ≥ 4.x | Adaptive Cards | Non | Azure App |
| REST | FastAPI | Brut (passthrough) | Oui | API Key |
| TUI | Textual | Standard | Non | Local |
| WhatsApp | Baileys (Node.js) | Strip | Oui | QR code |
| Signal | signal-cli | Strip | Non | Numéro dédié |

### Streaming progressif — édition temps réel

Pour les canaux supportant l'édition de messages (Discord, Telegram), L'Atelier publie les chunks token-par-token dans `relais:messages:streaming:{channel}:{correlation_id}`. L'Aiguilleur envoie un message placeholder, lit les chunks en boucle, et édite le message progressivement. Un flag `is_final` signale la fin du stream.

---

## 9. Le Portail — routage & politique de réponse

### Rôle

Le Portail valide le format Envelope entrant, met à jour le registre des sessions actives (`relais:active_sessions:{user_id}` — TTL 1h), applique la politique de réponse (DND, vacation, in_meeting) et route vers La Sentinelle.

### config/reply_policy.yaml

```yaml
global:
  default_mode: manual
  default_language: fr
  active_hours:
    start: "08:00"
    end: "22:00"
    timezone: "Europe/Paris"
  out_of_hours:
    mode: auto_immediate
    prompt: prompts/out_of_hours.md

channels:
  whatsapp:
    default_mode: auto_deferred
    debounce_delay: 120
    default_prompt: prompts/whatsapp_default.md
    notify_on_debounce: true
  telegram:
    default_mode: auto_immediate
    default_prompt: prompts/telegram_default.md
  signal:
    default_mode: manual
  discord:
    default_mode: auto_immediate
    default_prompt: prompts/discord_default.md
    condition: mention_only

senders:
  - id: "+33612345678"
    name: "Marie"
    mode: auto_deferred
    debounce_delay: 120
    prompt: prompts/marie.md
    active_hours: { start: "07:00", end: "23:00" }
    allowed_channels: [whatsapp, telegram]

  - id: "+33698765432"
    name: "Famille"
    mode: auto_immediate
    prompt: prompts/family.md

  - id: "client_acme@slack"
    name: "ACME Corp"
    mode: auto_deferred
    debounce_delay: 300
    prompt: prompts/professional_client.md
    escalation: { delay: 600, action: notify_urgent }

  - id: "bot_*"
    mode: ignore

  - id: unknown
    mode: manual
    welcome_message: >
      Bonjour ! Je suis l'assistant de Benjamin.
      Il vous répondra dès que possible.

overrides:
  - name: "Summer vacation"
    active: false
    start: "2026-07-15"
    end: "2026-08-15"
    global_mode: auto_immediate
    prompt: prompts/vacation.md

  - name: "Meeting mode"
    active: false
    duration_minutes: 60
    global_mode: auto_immediate
    prompt: prompts/in_meeting.md
```

---

## 10. La Sentinelle — sécurité & profils

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

### config/users.yaml

```yaml
users:
  - internal_id: usr_benjamin
    display_name: "Benjamin"
    role: ADMIN
    identities:
      telegram: "123456789"
      discord: "789012345678"
      rest_api_key_hash: "sha256:..."
    notification_strategy:
      normal: last_active     # 1 canal — évite le bruit
      high: all_active        # tous les canaux actifs
      critical: all_active    # tous les canaux + notif système OS
    active: true

  # Utilisateur système — sessions CRON et planifiées
  - internal_id: usr_system
    display_name: "RELAIS System"
    role: SCHEDULER_AGENT
    identities: {}
    notification_strategy:
      normal: last_active
      high: all_active
      critical: all_active
    notification_target_user: usr_benjamin
    active: true
```

### Politique utilisateur inconnu

```yaml
channels:
  telegram:
    unknown_user_policy: pending   # approbation admin manuelle
  whatsapp:
    unknown_user_policy: guest     # profil USER limité automatique
  rest:
    unknown_user_policy: deny      # rejet 401
```

---

## 11. L'Atelier — exécution des agents, résilience LLM, outils internes

### Architecture générale

L'Atelier suit ce flux pour chaque tâche entrante :

```
Incoming envelope
  ↓
Parse + load profile (model, max_turns, max_tokens, resilience)
  ↓
Request context from Souvenir (relais:memory:request stream)
  ↓
Assemble system prompt (SOUL + role + channel + policy + user_facts)
  ↓
Load MCP servers for profile (mcp_loader.load_for_sdk)
  ↓
Build internal tools list (make_skills_tools)
  ↓
Execute via AgentExecutor (boucle multi-tour explicite)
  ├─ Start MCP servers (stdio/SSE)
  ├─ Merge internal tools + MCP tools
  └─ Loop: stream → tool calls → results → next turn, until end_turn or max_turns
  ↓
If streaming capable (Discord/Telegram): publish chunks to relais:messages:streaming:{channel}:{correlation_id}
  ↓
Publish response to relais:messages:outgoing:{channel}
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
├── tools.py                # make_skills_tools() → list_skills + read_skill
├── mcp_loader.py           # Chargement config MCP servers
├── profile_loader.py       # ProfileConfig, ResilienceConfig
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

### Outils natifs — make_skills_tools()

`atelier/tools.py` expose deux outils pour la gestion des skills :

| Outil | Description |
|-------|-------------|
| `list_skills` | Scanne `skills_dir` récursivement pour les `SKILL.md`, retourne un catalogue `"- {nom}: {première ligne}"` |
| `read_skill(skill_name)` | Lit et retourne le contenu complet du `SKILL.md` correspondant |

### Serveurs MCP — McpSessionManager

`McpSessionManager` prend en charge deux transports : `stdio` (sous-processus) et `sse` (connexion HTTP). Si le package MCP est absent, le manager loggue un warning et retourne des listes vides sans crash — les outils internes restent fonctionnels dans tous les cas.

### ProfileConfig — champs clés

| Champ | Type | Description |
|-------|------|-------------|
| `model` | str | Format `provider:model-id` (ex: `anthropic:claude-sonnet-4-6`) |
| `temperature` | float | Température LLM |
| `max_tokens` | int | Tokens max par réponse |
| `max_turns` | int | Tours max boucle agentique (défaut: 20) |
| `mcp_timeout` | int | Timeout (s) par appel outil MCP (défaut: 10) |
| `mcp_max_tools` | int | Max outils MCP exposés au modèle (0 = aucun MCP) |
| `resilience` | ResilienceConfig | Retries, délais backoff, fallback model |

`mcp_timeout` annule un appel outil MCP dépassant le délai imparti et retourne une chaîne d'erreur au modèle sans interrompre la boucle. `mcp_max_tools` tronque la liste des outils MCP — les outils internes ne sont pas comptés dans cette limite.

### Profil `memory_extractor` — extraction légère de faits utilisateur

```yaml
# config/profiles.yaml.default
memory_extractor:
  model: glm-4.7-flash
  temperature: 0.1
  max_tokens: 512
  max_turns: 1
  stream: false
  memory:
    short_term_messages: 0
  allowed_tools: null
  allowed_mcp: null
  guardrails: []
  memory_scope: own
  fallback_model: null
  resilience:
    retry_attempts: 2
    retry_delays: [1, 3]
    fallback_model: null
```

Utilisé par Le Souvenir pour l'extraction automatique de faits utilisateur. Le modèle est chargé dynamiquement depuis ce profil au démarrage de Le Souvenir — changeable sans redéploiement.

### Résilience LLM — pattern XACK

```yaml
# config/profiles.yaml — section résilience dans chaque profil
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

- `AgentExecutionError` → DLQ (`relais:tasks:failed`) + ACK
- Exception transiente (réseau, timeout) → pas d'ACK → reste en PEL pour re-livraison
- Succès → ACK après publication dans `relais:messages:outgoing:{channel}`

---

## 12. Le Souvenir — mémoire, dual-stream, extraction de faits

### Deux niveaux de mémoire

```
Mémoire contexte  → Redis (volatile, TTL 24h)
                    Cache rapide pour l'historique conversationnel
                    Clé : relais:context:{session_id} (List Redis)

Mémoire longue    → SQLite (dev) → PostgreSQL (prod) via Alembic migrations
                    Messages archivés + faits utilisateur structurés
                    Tables: messages, user_facts
```

### Architecture dual-stream

Le Souvenir consomme deux streams en parallèle :

**Stream 1 : `relais:memory:request`** (Atelier → Souvenir)
- Action `get` : retourner l'historique pour une session donnée
- Flux : Atelier envoie `{action: "get", session_id, correlation_id}` → Souvenir répond via `relais:memory:response`

**Stream 2 : `relais:messages:outgoing:{channel}`** (observe toutes les réponses)
- Pour chaque message sortant : mettre en cache Redis, archiver en SQLite, extraire les faits utilisateur

### Flux mémoire : get (Atelier → Souvenir → Atelier)

```
1. Atelier → XADD relais:memory:request {action: "get", session_id, correlation_id}

2. Souvenir._handle_get_request():
   a. Try Redis List relais:context:{session_id} (cache, fast path)
   b. If miss → SQLite SELECT (fallback on Redis restart)
   c. XADD relais:memory:response {correlation_id, messages: [...]}

3. Atelier ← XREAD relais:memory:response (timeout 3s, filter by correlation_id)
```

### Flux mémoire : outgoing (observation + extraction)

```
1. relais:messages:outgoing:{channel} published by Atelier

2. Souvenir._handle_outgoing():
   a. Extract user message from envelope.metadata["user_message"]
   b. Extract assistant reply from envelope.content
   c. RPUSH relais:context:{session_id} [user_msg, assistant_reply]
      LTRIM -20, EXPIRE 24h
   d. long_term_store.archive(envelope)     ← SQLite messages
   e. memory_extractor.extract(envelope)    ← identification faits utilisateur
```

### Memory extraction — identification automatique de faits utilisateur

Le `MemoryExtractor` appelle un LLM léger (profil `memory_extractor`) pour identifier les faits durables sur l'utilisateur (préférences, contraintes, objectifs) à partir de chaque échange. Les faits avec une confiance > 0.7 sont stockés en SQLite dans la table `user_facts`. L'extraction est idempotente — un hash `(sender_id, fact)` évite les doublons.

**Table `user_facts` :**

| Colonne | Type | Description |
|---------|------|-------------|
| `id` | TEXT PK | Hash SHA256 de `sender_id:fact` |
| `sender_id` | TEXT | Identifiant expéditeur |
| `fact` | TEXT | Fait extrait |
| `category` | TEXT | `preference`, `constraint`, `goal`, `context` |
| `confidence` | REAL | Score 0.0–1.0 |
| `source_corr` | TEXT | `correlation_id` du message source |
| `created_at` | REAL | Epoch seconds |
| `updated_at` | REAL | Epoch seconds |

### Compaction contexte

Quand l'historique dépasse 80% du context window du modèle, un LLM léger (Haiku) génère un résumé qui remplace les anciens messages.

### Scopes mémoire

| Scope | Historique | Faits | Profils |
|---|---|---|---|
| `global` | Toutes sessions récentes | Benjamin + tous | ADMIN |
| `own` | Session courante | Benjamin | USER, SUPERVISOR |
| `sender` | Sessions avec l'expéditeur | Benjamin + expéditeur | AUTO_REPLY |
| `task` | Contexte parent uniquement | Aucun | SUB_AGENT |

### Pagination native

Le Souvenir expose une API de requête paginée sur la mémoire longue terme : `limit`, `offset`, `since`, `until`, `search`. Utilisée par Le Vigile pour les requêtes admin ("montre page 2 des logs d'hier").

---

## 13. L'Archiviste — pure observer avec pipeline observation

L'Archiviste est un observer pur — il consomme sans jamais publier.

**Deux consumer groups en parallèle :**

1. `archiviste_logs_group` — observe `relais:logs` (logs critiques de toutes les briques)
2. `archiviste_pipeline_group` — observe tous les streams du pipeline pour visibilité end-to-end

**Streams observés :**
- `relais:messages:incoming:*` (par canal)
- `relais:security`
- `relais:tasks`
- `relais:tasks:failed` (DLQ)
- `relais:messages:outgoing:*` (par canal)

**Pub/Sub :** `relais:events:*` (fire & forget)

**Enrichissement des logs :** Chaque entrée dans `relais:logs` est enrichie par les briques avec `correlation_id`, `sender_id`, et `content_preview` (60 premiers caractères). L'Archiviste préfixe les lignes de log avec `[{cid[:8]}] {sender_id} | message`.

**Rétention :** L'Archiviste traite les commandes `SYSTEM:cleanup_logs` du Veilleur — suppression des JSONL selon `retention.jsonl_days`, purge SQLite selon `retention.sqlite_days`, les logs d'audit ne sont jamais supprimés.

---

## 24. Le Commandant — commandes globales hors-LLM

### Rôle

Le Commandant intercepte les messages textuels commençant par `/` avant qu'ils n'atteignent le pipeline LLM. Il exécute l'action demandée immédiatement et répond directement à l'utilisateur sur le canal d'origine. Aucun token LLM n'est consommé.

**Taxonomie :** Transformer — consomme `relais:messages:incoming`, publie vers `relais:messages:outgoing:{channel}` et/ou `relais:memory:request`.

### Architecture

```
relais:messages:incoming
  ├─► [Le Portail]      (portail_group)      — pipeline normal
  └─► [Le Commandant]   (commandant_group)   — interception des commandes
```

Le Commandant possède son propre consumer group sur `relais:messages:incoming`. Pour chaque message lu :
- Si le contenu commence par `/` → intercepte, traite, ACK, répond sur `outgoing:{channel}`
- Sinon → ACK immédiatement sans action (le Portail traite de son côté dans son groupe)

Le pipeline normal n'est jamais perturbé : les deux consumer groups sont indépendants.

### Commandes supportées

| Commande | Description | Portée |
|---|---|---|
| `/clear` | Efface l'historique de la session courante | Session courante |
| `/dnd` | Active le mode "Do Not Disturb" — suspend toutes les réponses LLM | Global (tous canaux) |
| `/brb` | Désactive le mode DND — reprend le pipeline normal | Global (tous canaux) |

### Comportement détaillé

#### `/clear`

1. Le Commandant publie `{"action": "clear", "session_id": <session_id>}` sur `relais:memory:request`
2. Le Souvenir efface :
   - Le contexte Redis court terme (`relais:context:{session_id}`)
   - Les messages SQLite de la session (table `messages`)
   - Les `user_facts` sont **conservés**
3. Le Commandant répond sur `relais:messages:outgoing:{channel}` : `"Historique effacé."`

#### `/dnd`

1. Le Commandant écrit `SET relais:state:dnd 1` dans Redis (sans TTL — persistant jusqu'au `/brb`)
2. Le Portail consulte cette clé avant tout routage. Si présente : ACK le message sans le transmettre à La Sentinelle
3. Le Commandant répond : `"Mode silencieux activé. Utilisez /brb pour reprendre."`

#### `/brb`

1. Le Commandant exécute `DEL relais:state:dnd`
2. Le pipeline reprend normalement dès le message suivant
3. Le Commandant répond : `"De retour ! Je vous écoute."`

### Accès Redis requis

| Opération | Stream / Clé |
|---|---|
| XREADGROUP (lecture) | `relais:messages:incoming` |
| XADD (réponse canal) | `relais:messages:outgoing:*` |
| XADD (effacement mémoire) | `relais:memory:request` |
| SET / DEL (état DND) | `relais:state:dnd` |

Le Portail doit avoir accès en lecture à `relais:state:dnd` (GET).

### Extensibilité

Toute nouvelle commande hors-LLM s'ajoute dans Le Commandant sans toucher aux autres briques. Le registre des commandes est un dictionnaire `{"/nom": handler_function}` — ajout en O(1).

Les commandes inconnues sont ignorées silencieusement (ACK + pas de réponse) afin de ne pas bloquer les messages légitimes commençant par `/` sur certains canaux (ex: commandes Slack natives).

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

L'Atelier assemble le system prompt en 6 couches, séparées par `\n\n---\n\n` :

```
prompts/
├── soul/
│   └── SOUL.md                    ← Layer 1: Core personality (always attempted)
├── roles/
│   ├── admin.md                   ← Layer 2: Role-specific instructions
│   └── user.md
├── users/                         ← Layer 3: Per-user overrides (created by user)
│   └── discord_12345_678.md
├── channels/
│   ├── discord_default.md         ← Layer 4: Channel formatting rules
│   ├── telegram_default.md
│   └── whatsapp_default.md
└── policies/
    ├── in_meeting.md              ← Layer 5: Active reply policy overlay
    ├── out_of_hours.md
    └── vacation.md
```

**Ordre d'assemblage :**

| Ordre | Source | Fichier | Toujours présent |
|-------|--------|------|---|
| 1 | Personality | `soul/SOUL.md` | Oui (erreur si absent) |
| 2 | Role | `prompts/roles/{role}.md` | Non (optionnel) |
| 3 | User | `prompts/users/{sender_id}.md` | Non (optionnel) |
| 4 | Channel | `prompts/channels/{channel}_default.md` | Non (warning si absent) |
| 5 | Policy | `prompts/policies/{reply_policy}.md` | Non (optionnel) |
| 6 | Memory | Faits utilisateur injectés depuis SQLite | Non (optionnel) |

### Internationalisation — SOUL.md gère tout

SOUL.md instruit JARVIS d'utiliser la langue de son interlocuteur. Le LLM détecte automatiquement la langue entrante et répond dans la même langue. Les notifications système natives restent en français car elles sont générées par Le Crieur, pas par le LLM.

### Construction du prompt final

```
Couche 1 : SOUL.md           si apply_soul=true        ~500 tokens
Couche 2 : Mémoire longue    si memory.long_term=true   0-1000 tokens
Couche 3 : Historique        si memory.context=true      0-2000 tokens
           (compacté si > 80% context window)
Couche 4 : Prompt de tâche   toujours présent           ~200 tokens
─────────────────────────────────────────────────────────────────────
Message utilisateur                                      N tokens
```

---

## 16. Profils — config/profiles.yaml complet

```yaml
profiles:

  # ── Profils humains — SOUL appliqué ──────────────────────────────────────

  ADMIN:
    type: human
    apply_soul: true
    model: anthropic:claude-opus-4-6
    memory: { context: true, long_term: true, scope: global }
    allowed_tools: ["*"]
    allowed_mcp: ["*"]
    sub_agent_limits: { max_depth: 2, max_token_budget: 50000 }
    resilience:
      retry_attempts: 3
      retry_delays: [2, 5, 15]
      fallback_model: ollama:llama3.2
    guardrails:
      max_tokens_per_day: null
      forbidden_bash_patterns: []
      require_confirmation: [stop_all, revoke_user, delete_memory]

  SUPERVISOR:
    type: human
    apply_soul: true
    model: anthropic:claude-sonnet-4-6
    memory: { context: true, long_term: true, scope: own }
    allowed_tools: [Read, "Bash(git *)", "Bash(docker ps*)", "Bash(docker logs*)"]
    allowed_mcp: ["mcp__gitlab__*", mcp__brave__search, mcp__jcodemunch__read_file]
    sub_agent_limits: { max_depth: 1, max_token_budget: 20000 }
    resilience:
      retry_attempts: 3
      retry_delays: [2, 5, 15]
      fallback_model: ollama:llama3.2
    guardrails:
      max_tokens_per_day: 200000
      forbidden_bash_patterns: ["rm *", "sudo *", "curl * | bash"]
      require_confirmation: ["restart:*"]

  USER:
    type: human
    apply_soul: true
    model: anthropic:claude-haiku-4-5
    memory: { context: true, long_term: true, scope: own }
    allowed_tools: [Read]
    allowed_mcp: [mcp__brave__search]
    sub_agent_limits: { max_depth: 0, max_token_budget: 0 }
    resilience:
      retry_attempts: 3
      retry_delays: [2, 5, 15]
      fallback_model: ollama:llama3.2
      fallback_message: "Service momentanément limité, je continue avec un modèle local."
    guardrails:
      max_tokens_per_day: 50000
      forbidden_bash_patterns: ["*"]
      forbidden_topics: [credentials, "internal system"]

  # ── Profils conversationnels — SOUL appliqué ──────────────────────────────

  AUTO_REPLY:
    type: auto_reply
    apply_soul: true
    model: anthropic:claude-sonnet-4-6
    memory: { context: true, long_term: true, scope: sender }
    allowed_tools: [Read, mcp__calendar__read_agenda, mcp__brave__search]
    allowed_mcp: ["mcp__calendar__*", mcp__brave__search]
    sub_agent_limits: { max_depth: 0, max_token_budget: 0 }
    resilience:
      retry_attempts: 3
      retry_delays: [2, 5, 15]
      fallback_model: ollama:llama3.2
    guardrails:
      max_tokens_per_response: 500
      forbidden_topics: [credentials, "banking data"]
      mandatory_signature: "— JARVIS, assistant de Benjamin"
      max_sub_agents: 0

  # ── Profils techniques silencieux — pas de SOUL ───────────────────────────

  SUB_AGENT:
    type: technical
    apply_soul: false
    model: ollama:qwen3-coder-30b
    memory: { context: true, long_term: false, scope: task }
    allowed_tools: [Read, "Bash(git *)", "mcp__jcodemunch__*"]
    allowed_mcp: ["mcp__jcodemunch__*", mcp__gitlab__get_mr]
    sub_agent_limits: { max_depth: 0, max_token_budget: 0 }
    resilience:
      retry_attempts: 2
      retry_delays: [1, 3]
      fallback_model: null
    guardrails:
      max_tokens_per_turn: 2000

  SCHEDULER_AGENT:
    type: technical
    apply_soul: false
    model: anthropic:claude-haiku-4-5
    memory: { context: false, long_term: true, scope: global }
    allowed_tools: [Read, "mcp__gitlab__*", "mcp__calendar__*", mcp__brave__search]
    allowed_mcp: ["*"]
    sub_agent_limits: { max_depth: 1, max_token_budget: 10000 }
    resilience:
      retry_attempts: 3
      retry_delays: [2, 5, 15]
      fallback_model: ollama:llama3.2
    guardrails:
      max_tokens_per_run: 5000

  LEARNER_AGENT:
    type: technical
    apply_soul: false
    model: anthropic:claude-sonnet-4-6
    memory: { context: false, long_term: false, scope: task }
    allowed_tools: [Read, Write]
    allowed_mcp: []
    sub_agent_limits: { max_depth: 0, max_token_budget: 0 }
    resilience:
      retry_attempts: 3
      retry_delays: [2, 5, 15]
      fallback_model: null
    guardrails:
      max_tokens_per_run: 10000

  memory_extractor:
    model: glm-4.7-flash
    temperature: 0.1
    max_tokens: 512
    max_turns: 1
    stream: false
    memory:
      short_term_messages: 0
    allowed_tools: null
    allowed_mcp: null
    guardrails: []
    memory_scope: own
    resilience:
      retry_attempts: 2
      retry_delays: [1, 3]
      fallback_model: null
```

---

## 17. Politique de réponse automatique

Voir section 9. Résumé des modes :

| Mode | Comportement |
|---|---|
| `ignore` | Archivé silencieusement |
| `manual` | Notification owner uniquement |
| `auto_immediate` | Réponse JARVIS sans délai |
| `auto_deferred` | Attente N sec, puis JARVIS |

### Suspension dynamique via commandes

Le mode DND ("Do Not Disturb") peut être activé et désactivé à la volée par l'utilisateur via des commandes textuelles (voir section 24). Lorsque DND est actif, Le Portail supprime silencieusement tous les messages entrants avant tout routage vers La Sentinelle — aucune réponse LLM n'est produite.

| Commande | Effet |
|---|---|
| `/dnd` | Active le mode DND global — Le Portail ignore tous les messages |
| `/brb` | Désactive le mode DND — reprise normale du pipeline |

---

## 18. Gestion des médias

### Principe

Les médias (images, audio, documents) sont stockés temporairement dans `~/.relais/media/` avec un TTL de 24h. L'agent ne voit pas directement le fichier — une référence locale (`MediaRef`) est injectée dans l'Envelope et transformée en section dans le prompt.

**Champ `media_refs` de l'Envelope :**

| Champ | Type | Description |
|-------|------|-------------|
| `media_id` | str | UUID unique |
| `path` | str | Chemin local dans `~/.relais/media/` |
| `mime_type` | str | Type MIME |
| `size_bytes` | int | Taille en octets |
| `expires_in_hours` | int | TTL (24h) |

### Nettoyage

Le Veilleur nettoie les fichiers médias expirés lors de son tick quotidien (`SYSTEM:cleanup_media`). Les fichiers dont la clé Redis `relais:media:{media_id}` a expiré sont supprimés.

---

## 19. Système d'extensions

```
INTERCEPTEUR (tisserand/)  In-process · Python · 2s timeout · return None = blocage
OBSERVER (relais:events:*) Out-of-process · Tout langage · Fire & forget
```

**Règle de décision fondamentale :**
```
"Peut bloquer ?"  → intercepteur (Le Tisserand)
"Observe ?"       → Redis event  (out-of-process)
```

### Intercepteurs natifs

| Extension | Priority | Rôle |
|---|---|---|
| `quota-enforcer` | 5 | Bloque si quota dépassé (Redis INCR + TTL minuit) |
| `content-filter` | 1 | Bloque patterns dangereux |
| `custom-tools` | 50 | Injecte MCP supplémentaires par utilisateur |

**Interface intercepteur :** `on_request`, `on_stream_chunk`, `on_tool_call_start`, `on_command` — timeout 2s par intercepteur, exception → skippé + loggé, RELAIS ne crashe jamais à cause d'une extension.

Observer tiers — aucun SDK requis, juste un client Redis avec compte ACL. Exemples dans `observers/`.

---

## 20. Sécurité

### .env — secrets complets

```bash
# LLM providers
ANTHROPIC_API_KEY=sk-ant-...
OPENROUTER_API_KEY=sk-or-...

# Messaging bridges
TELEGRAM_BOT_TOKEN=...
DISCORD_BOT_TOKEN=...
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...

# REST canal
REST_API_KEY=...

# Redis
REDIS_PASSWORD=...
REDIS_PASS_AIGUILLEUR=...
REDIS_PASS_PORTAIL=...
REDIS_PASS_SENTINELLE=...
REDIS_PASS_ATELIER=...
REDIS_PASS_SOUVENIR=...
REDIS_PASS_VEILLEUR=...
REDIS_PASS_CRIEUR=...
REDIS_PASS_FORGERON=...
REDIS_PASS_ARCHIVISTE=...
REDIS_PASS_VIGILE=...
REDIS_PASS_SCRUTATEUR=...
REDIS_PASS_TISSERAND=...

# MCP servers
GITLAB_TOKEN=...
GITLAB_URL=https://gitlab.company.com
BRAVE_API_KEY=...
GOOGLE_CREDENTIALS_PATH=/opt/relais/config/google_credentials.json

# Webhooks HMAC
WEBHOOK_SECRET_GITHUB=...
WEBHOOK_SECRET_GITLAB=...
WEBHOOK_SECRET_GRAFANA=...

# Backup
BACKUP_PATH=/Volumes/Backup/relais
```

### Graceful shutdown

Chaque brique implémente `GracefulShutdown` : handlers SIGTERM/SIGINT, tracking des tâches async en vol, attente de fin avant exit. Le `stopwaitsecs` supervisord doit être supérieur au timeout Python de chaque brique.

---

## 21. Structure du projet

```
/opt/relais/                           ← Installation système (code uniquement)
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
│   ├── profiles.yaml.default
│   ├── users.yaml.default
│   ├── reply_policy.yaml.default
│   ├── mcp_servers.yaml.default
│   ├── redis.conf
│   └── HEARTBEAT.md.default
│
├── soul/                              ← Templates SOUL par défaut
│   ├── SOUL.md.default
│   └── variants/
│       ├── SOUL_concise.md.default
│       └── SOUL_professional.md.default
│
├── prompts/                           ← Prompts système par défaut
│   ├── channels/
│   │   ├── discord_default.md
│   │   ├── telegram_default.md
│   │   └── whatsapp_default.md
│   ├── policies/
│   │   ├── out_of_hours.md
│   │   ├── vacation.md
│   │   └── in_meeting.md
│   └── roles/
│       ├── admin.md
│       └── user.md
│
├── common/
│   ├── envelope.py                    ← Envelope + PushEnvelope + MediaRef
│   ├── redis_client.py
│   ├── stream_client.py
│   ├── event_publisher.py
│   ├── shutdown.py
│   ├── health.py
│   ├── config_loader.py               ← get_relais_home() + resolve_config_path()
│   ├── init.py                        ← initialize_user_dir()
│   └── markdown_converter.py
│
├── aiguilleur/
│   ├── main.py                        ← AiguilleurManager (processus unifié)
│   ├── base.py                        ← AiguilleurBase ABC
│   ├── channel_config.py
│   └── channels/
│       ├── discord/adapter.py
│       ├── telegram/adapter.py
│       ├── slack/adapter.py
│       └── rest/adapter.py
│
├── portail/
│   ├── main.py
│   ├── reply_policy.py
│   └── prompt_loader.py
│
├── sentinelle/
│   └── main.py
│
├── atelier/
│   ├── main.py
│   ├── agent_executor.py
│   ├── mcp_adapter.py
│   ├── mcp_session_manager.py
│   ├── tools.py                       ← list_skills + read_skill
│   ├── mcp_loader.py
│   ├── profile_loader.py
│   ├── soul_assembler.py
│   └── stream_publisher.py
│
├── souvenir/
│   ├── main.py
│   ├── context_store.py
│   ├── long_term_store.py
│   ├── memory_extractor.py
│   ├── models.py
│   └── migrations/
│
├── archiviste/
│   └── main.py
│
├── mcp/                               ← MCP servers globaux supervisord
│   ├── calendar/server.py
│   └── brave-search/server.js
│
├── extensions/                        ← Intercepteurs Le Tisserand
│   ├── quota-enforcer/
│   ├── content-filter/
│   └── custom-tools/
│
├── observers/                         ← Exemples observers out-of-process
│   ├── example_python.py
│   ├── example_node.js
│   └── example_go.go
│
└── tests/
    ├── unit/
    ├── integration/
    └── e2e/

─────────────────────────────────────────────────────────────────────

~/.relais/                             ← Répertoire utilisateur (données & config)
│                                         Résolu par get_relais_home()
│                                         Override via RELAIS_HOME=...
│                                         Créé automatiquement au 1er lancement
├── config/
│   ├── config.yaml
│   ├── profiles.yaml
│   ├── users.yaml
│   ├── reply_policy.yaml
│   ├── mcp_servers.yaml
│   └── HEARTBEAT.md
│
├── soul/
│   ├── SOUL.md
│   └── variants/
│       ├── SOUL_concise.md
│       └── SOUL_professional.md
│
├── prompts/
│   ├── marie.md
│   └── family.md
│
├── skills/
│   ├── CLAUDE.md                      ← registre skills actifs
│   ├── manual/
│   │   └── SKILL_my_custom.md
│   └── auto/
│       └── SKILL_auto_mr_review_20260327.md
│
├── media/                             ← TTL 24h
│
├── logs/
│   ├── relais.db
│   └── YYYY-MM-DD.jsonl
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
│  5.  ~/.relais/ = config + skills + logs + médias               │
│      /opt/relais/ = code système uniquement                     │
│      Cascade : ~/.relais/ > /opt/relais/ > ./                   │
│      Override via RELAIS_HOME=... si nécessaire                 │
│      initialize_user_dir() crée ~/.relais/ au 1er lancement     │
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
│  18. usr_system dans users.yaml — sessions CRON → Benjamin     │
│  19. UN PROFIL = model + tools + memory + resilience + limits  │
│  20. LLM RESILIENCE : retry 3× backoff → fallback Ollama       │
│  21. SOUL si apply_soul=true — i18n géré par SOUL.md seul      │
│  22. PROMPT = SOUL + long-term + history (compacté) + task     │
│  23. COMPACTION à 80% context window — Haiku génère le résumé  │
│  24. SOUS-AGENTS : max profondeur 2 + budget tokens par tâche  │
│                                                                  │
│  MÉDIAS & DONNÉES                                               │
│  25. MÉDIAS stockés dans ~/.relais/media/ TTL 24h              │
│  26. SKILLS versionnés par nom de fichier — anciens conservés  │
│  27. BACKUP configurable — rsync + SQLite .backup() API        │
│  28. RÉTENTION configurable — JSONL 90j, SQLite 1 an, audit ∞  │
│  29. PAGINATION native dans Le Souvenir — limit/offset         │
│  30. MARKDOWN converti par L'Aiguilleur à la sortie            │
│                                                                  │
│  EXTENSIBILITÉ                                                   │
│  31. INTERCEPTEURS via Le Tisserand — in-process — 2s timeout  │
│  32. OBSERVERS via Redis events — out-of-process — tout langage│
│  33. REST API : API Key + FastAPI /docs auto-générée           │
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
│      "OÙ EST LA DATA?"→ ~/.relais/ (cascade depuis /opt/relais)│
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

**Taxonomie :** Batch Processor — lit SQLite L'Archiviste, génère des skills, publie, exit.

**Rôle :** Identifie les patterns répétés dans les logs de L'Archiviste, génère des `SKILL.md` automatiques, les publie dans `relais:skills:new`. Lancé quotidiennement par Le Veilleur (`SYSTEM:start_forgeron`), `autostart=false`, `autorestart=false`.

**Versioning des skills — par nom de fichier :**

```
Pas de Git, pas de SQLite de versioning.
Les fichiers auto-générés sont nommés avec la date et jamais supprimés.

~/.relais/skills/auto/
  SKILL_auto_mr_review_20260327.md    ← version courante dans CLAUDE.md
  SKILL_auto_mr_review_20260315.md    ← version précédente — conservée
  SKILL_auto_mr_review_20260301.md    ← version encore plus ancienne

Rollback :
  Le Vigile modifie CLAUDE.md pour pointer vers une version antérieure.
  → La prochaine session charge l'ancienne version
```

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
