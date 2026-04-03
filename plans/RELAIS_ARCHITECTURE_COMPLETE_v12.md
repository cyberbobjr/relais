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
│   ├── portail.yaml              ← registry utilisateurs + rôles (Portail)
│   ├── sentinelle.yaml           ← ACL Sentinelle (access_control + groups)
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
| Le Portail | Charge `~/.relais/config/portail.yaml` (UserRegistry — users + rôles fusionnés) |
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
| 🛡️ **La Sentinelle** | `sentinelle/` | Transformer | ACL, profils, guardrails (bidirectionnel) | ← `security` / → `tasks` ; ← `outgoing_pending` / → `outgoing:{ch}` |
| 📨 **Le Coursier** | Redis | Infrastructure | Bus messages Unix socket | — |
| ⚒️ **L'Atelier** | `atelier/` | Stream Consumer | Exécution agents LLM | ← `tasks` / → `outgoing_pending`, `tasks:failed` |
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
| 🎮 **Le Commandant** | `commandant/` | Transformer | Commandes globales hors-LLM (`/clear`) |

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
> **Filtrage MCP par rôle** — les outils MCP exposés au modèle sont filtrés par `ToolPolicy.filter_mcp_tools()` selon les patterns `allowed_mcp_tools` définis dans `portail.yaml:roles:`. Les champs `mcp_timeout` (défaut 10 s) et `mcp_max_tools` (défaut 20) existent en tant que champs optionnels dans `ProfileConfig` mais ne sont plus documentés dans `profiles.yaml`.

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
  relais:messages:incoming:{channel}    Aiguilleur → Portail
  relais:messages:outgoing_pending            Atelier → Sentinelle (outgoing pass-through)
  relais:messages:outgoing:{channel}    Sentinelle → Aiguilleur + Souvenir (observer)
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
- `profile` — profil LLM appliqué à tous les messages du canal (optionnel) ; stampé dans `envelope.metadata["llm_profile"]` par l'Aiguilleur au moment de la création de l'enveloppe entrante ; si absent, l'Aiguilleur lit `config.yaml > llm.default_profile` (fallback `"default"`)

> **Responsabilité du stamping :** c'est l'**Aiguilleur** (adaptateur de canal) qui stampe `envelope.metadata["llm_profile"]` — pas la Sentinelle. La Sentinelle n'écrit jamais ce champ.
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

Le Portail valide le format Envelope entrant, résout l'utilisateur via `UserRegistry` et `RoleRegistry` (portail.yaml), enrichit l'enveloppe avec les métadonnées contextuelles (`user_role`, `display_name`, `llm_profile`, `custom_prompt_path`, `skills_dirs`, `allowed_mcp_tools`), met à jour le registre des sessions actives (`relais:active_sessions:{user_id}` — TTL 1h), applique la politique de réponse (vacation, in_meeting) et route **tout message accepté** vers La Sentinelle — y compris les commandes slash. Le Portail ne filtre pas les commandes et ne gère pas d'état DND.

**`unknown_user_policy`** (champ `config.yaml:security:unknown_user_policy`) :
- `deny` (défaut) : drop silencieux
- `guest` : stamp identité guest synthétique, rôle `guest` (accès restreint)
- `pending` : publie l'enveloppe sur `relais:admin:pending_users` pour validation manuelle, puis drop

---

## 10. La Sentinelle — sécurité & profils

### Séparation des responsabilités : enrichissement vs sécurité

**Architecture clarifiée :**

1. **Le Portail** effectue l'**enrichissement contextuel** : résout l'utilisateur depuis `portail.yaml`, stampe `envelope.metadata["user_record"]` (dict `UserRecord` fusionné — rôle + utilisateur, incluant `user_id`) et `envelope.metadata["user_id"]` (raccourci stable cross-canal, égal à la clé YAML, ex : `"usr_admin"`). Portail est le **seul écrivain** de l'identité utilisateur. `user_id` permet aux briques aval (Souvenir, Atelier) de reprendre une conversation quelque soit le canal d'origine sans connaître le `sender_id` spécifique au canal.
2. **La Sentinelle** effectue la **sécurité pure et le routage des commandes** : lit `user_record` depuis `envelope.metadata` (chargé par Portail), vérifie `blocked`, bifurque commandes et messages normaux, applique guardrails (bidirectionnel). Config ACL dans `sentinelle.yaml`.

> **Note architecture :** La Sentinelle n'effectue **pas** d'enrichissement.
> En flux entrant, elle valide l'ACL et bifurque :
> - **Message normal** → publie vers `relais:tasks`
> - **Commande connue + autorisée** (`action=<cmd>` dans le rôle) → publie vers `relais:commands`
> - **Commande inconnue** → réponse inline `"Commande inconnue : /xxx"`, ACK sans forward
> - **Commande non autorisée** → réponse inline `"Vous n'avez pas la permission d'exécuter /xxx"`, ACK sans forward
>
> En flux sortant, elle consomme `relais:messages:outgoing_pending`, applique les guardrails sortants, et publie vers `relais:messages:outgoing:{channel}`.

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
    llm_profile: fast

users:
  usr_benjamin:
    display_name: "Benjamin"
    role: admin
    blocked: false
    llm_profile: precise
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
    llm_profile: default
    notes: "Compte interne — ne pas supprimer"

roles:
  admin:
    actions: ["*"]       # "*" = toutes les commandes slash autorisées
  user:
    actions: []          # [] = aucune commande slash autorisée (messages normaux OK via default_mode)
```

### Politique utilisateur inconnu

La politique est un paramètre global sur `ACLManager` (pas par canal dans `channels.yaml`).

```
unknown_user_policy: "deny"     # rejet silencieux (défaut)
unknown_user_policy: "guest"    # accès avec profil LLM limité (guest_profile)
unknown_user_policy: "pending"  # rejet + notification dans relais:admin:pending_users
```

Le mode `blocklist` rend cette politique sans effet : les inconnus sont admis par défaut.

---

## 11. L'Atelier — exécution des agents, résilience LLM, outils internes

### Architecture générale

L'Atelier suit ce flux pour chaque tâche entrante :

```
Incoming envelope
  ↓
Parse + load profile
  — lit envelope.metadata["llm_profile"] (stampé par l'Aiguilleur)
  — si absent : fallback sur config.yaml > llm.default_profile → "default"
  — résout le ProfileConfig depuis profiles.yaml (model, max_turns, max_tokens, resilience)
  ↓
Request context from Souvenir (relais:memory:request stream)
  ↓
Assemble system prompt (SOUL + role + channel + policy + user_facts)
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
If streaming capable (Discord/Telegram): publish chunks to relais:messages:streaming:{channel}:{correlation_id}
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
| `resilience` | ResilienceConfig | Retries, délais backoff, fallback model |

> `allowed_tools`, `allowed_mcp`, `guardrails`, `memory_scope` sont retirés de `ProfileConfig` — voir `portail.yaml:roles:` pour le contrôle d'accès par rôle.

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
- Succès → ACK après publication dans `relais:messages:outgoing_pending`

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
1. relais:messages:outgoing:{channel} published by Sentinelle (Atelier → outgoing_pending → Sentinelle → outgoing)

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

Le Commandant exécute les commandes slash autorisées hors-LLM. Il reçoit uniquement les enveloppes pré-filtrées par La Sentinelle : commandes connues **et** autorisées pour le rôle de l'utilisateur. Il exécute l'action demandée immédiatement et répond directement à l'utilisateur sur le canal d'origine. Aucun token LLM n'est consommé.

**Taxonomie :** Transformer — consomme `relais:commands`, publie vers `relais:messages:outgoing:{channel}` et/ou `relais:memory:request`.

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

Le Commandant consomme `relais:commands` (`commandant_group`). Toutes les enveloppes arrivant ici ont déjà passé l'ACL identité et l'ACL commande. Le Commandant ACK chaque message qu'il dépile, qu'un handler existe ou non.

### Commandes supportées

| Commande | Description | Portée |
|---|---|---|
| `/clear` | Efface l'historique de la session courante | Session courante |

### Comportement détaillé

#### `/clear`

1. Le Commandant publie `{"action": "clear", "session_id": <session_id>, "envelope_json": <envelope serialisée>}` sur `relais:memory:request`
2. Le Souvenir efface :
   - Le contexte Redis court terme (`relais:context:{session_id}`)
   - Les messages SQLite de la session (table `messages`)
   - Les `user_facts` sont **conservés**
3. Le Souvenir répond sur `relais:messages:outgoing:{channel}` : `"✓ Historique de conversation effacé."` (confirmation envoyée par `ClearHandler` après le vrai nettoyage, si `envelope_json` est présent dans la requête)

### Accès Redis requis

| Opération | Stream / Clé |
|---|---|
| XREADGROUP (lecture) | `relais:commands` |
| XADD (réponse canal) | `relais:messages:outgoing:*` |
| XADD (effacement mémoire) | `relais:memory:request` |

### Extensibilité

Toute nouvelle commande hors-LLM s'ajoute dans Le Commandant sans toucher aux autres briques. Le registre des commandes est un dictionnaire `{"/nom": handler_function}` — ajout en O(1). Les commandes doivent également être déclarées dans `KNOWN_COMMANDS` (`common/command_utils.py`) pour être reconnues par La Sentinelle.

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
│   ├── admin.md                   ← Layer 2: Role overlay (path depuis users.yaml:roles:prompt_path)
│   └── user.md
├── users/                         ← Layer 3: Per-user override (chemin explicite depuis users.yaml:custom_prompt_path)
│   └── discord_12345_678.md
└── channels/
    ├── discord_default.md         ← Layer 4: Channel formatting rules
    ├── telegram_default.md
    └── whatsapp_default.md
```

**Ordre d'assemblage :**

| Ordre | Source | Fichier | Toujours présent |
|-------|--------|------|---|
| 1 | Personality | `soul/SOUL.md` | Oui (erreur si absent) |
| 2 | Role | `prompts/roles/{role}.md` (via `users.yaml:roles:prompt_path`) | Non (optionnel) |
| 3 | User | chemin explicite `users.yaml:custom_prompt_path` | Non (optionnel, warning si fichier absent) |
| 4 | Channel | `prompts/channels/{channel}_default.md` | Non (warning si absent) |

> **Note :** Les couches 5 (reply_policy) et 6 (user_facts) ont été retirées de l'assembleur. La politique de réponse est gérée en amont par le Portail (`reply_policy` dans `users.yaml:users:`) ; les user_facts ne sont plus injectés dans le system prompt.

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

> **Migration architecture (2026-04-01) :** Les champs `allowed_tools`, `allowed_mcp`, `guardrails`, `memory_scope` ont été **retirés de `ProfileConfig`** (profiles.yaml). Ces contraintes d'accès sont désormais définies par **rôle** dans `users.yaml:roles:` (champs `skills_dirs`, `allowed_mcp_tools`, `prompt_path`) et stampées par le Portail via `RoleRegistry`. Les exemples YAML ci-dessous reflètent les profils de conception originale — la structure réelle de `profiles.yaml` ne contient plus ces champs.

### Règle de résolution du profil

Le profil actif pour un message entrant est résolu dans cet ordre strict (le premier trouvé gagne) :

1. **`channels.yaml:profile`** — profil défini sur le canal d'origine (stampé par l'Aiguilleur dans `envelope.metadata["channel_profile"]`, résolu par le Portail)
2. **`users.yaml:llm_profile`** — préférence par utilisateur (portail.yaml, champ optionnel ; `null` = pas de préférence)
3. **`roles.<role>.llm_profile`** — préférence par rôle (portail.yaml, champ optionnel)
4. **`config.yaml > llm.default_profile`** — profil système par défaut
5. **`"default"`** — valeur de repli ultime si `llm.default_profile` est absent de `config.yaml`

> La résolution est effectuée par le **Portail** (`portail/main.py`) et le résultat est stampé dans `envelope.metadata["llm_profile"]` sur le stream `relais:security`. Le champ `user.llm_profile` dans portail.yaml est donc **actif** — `null` signifie "pas de préférence, laisser le canal ou le rôle décider".

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
│   ├── shutdown.py
│   ├── config_loader.py               ← get_relais_home() + resolve_config_path()
│   ├── init.py                        ← initialize_user_dir()
│   ├── user_registry.py               ← UserRegistry + UserRecord
│   ├── role_registry.py               ← RoleRegistry + RoleConfig (skills_dirs, allowed_mcp_tools)
│   ├── profile_loader.py              ← ProfileConfig + ResilienceConfig (shared by Atelier + Souvenir)
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
│   └── main.py
│
├── sentinelle/
│   ├── main.py
│   ├── acl.py                         ← ACLManager context-aware (allowlist/blocklist)
│   └── guardrails.py                  ← ContentFilter (pré/post-LLM)
│
├── atelier/
│   ├── main.py
│   ├── agent_executor.py
│   ├── mcp_adapter.py
│   ├── mcp_session_manager.py
│   ├── tool_policy.py                 ← ToolPolicy — skills + MCP access enforcement
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
