# RELAIS

RELAIS est une architecture micro-briques pour assistant IA, orchestrée via Redis Streams. Ce README décrit l'état réellement implémenté dans le code du dépôt aujourd'hui.

---

## Vue d'ensemble

Les briques actives du repo sont :

- `aiguilleur` : adaptateurs de canaux entrants/sortants
- `portail` : validation d'enveloppe + résolution d'identité
- `sentinelle` : ACL et routage messages/commandes
- `atelier` : exécution LLM via DeepAgents/LangGraph
- `commandant` : commandes slash hors LLM
- `souvenir` : mémoire court terme Redis + archivage SQLite
- `archiviste` : logs et observation partielle du pipeline
- `forgeron` : amélioration autonome des skills (réécriture directe via SkillEditor) et création automatique de skills depuis les archives
- `horloger` : planificateur CRON — déclenche des prompts programmés comme messages utilisateurs virtuels à travers le pipeline complet

Adaptateurs de canaux réellement livrés :

- **Discord** : adaptateur natif Python complet (`aiguilleur/channels/discord/adapter.py`)
- **WhatsApp** : adaptateur natif Python complet via la passerelle [fazer-ai/baileys-api](https://github.com/fazer-ai/baileys-api) (`aiguilleur/channels/whatsapp/adapter.py`) — serveur webhook aiohttp + client HTTP vers la passerelle. Installation, configuration et pairing via CLI (`python -m aiguilleur.channels.whatsapp install|configure|uninstall`) ou via les tools LangChain `whatsapp_install`, `whatsapp_configure`, `whatsapp_uninstall` du sous-agent `relais-config`. Voir [docs/WHATSAPP_SETUP.md](docs/WHATSAPP_SETUP.md).
- **REST** : adaptateur HTTP/JSON + SSE (`aiguilleur/channels/rest/adapter.py`) — expose `POST /v1/messages` (Bearer API key) et un stream SSE pour les clients programmatiques (CLI, CI, TUI). Playground SSE interactif sur `GET /docs/sse`. Authentification via clés API HMAC-SHA256 déclarées dans `portail.yaml`.

La configuration de canaux prévoit aussi `telegram` et `slack`, mais leur présence dans les fichiers de config ne signifie pas qu'un adaptateur complet existe forcément dans ce dépôt.

Outils livrés dans le dépôt :

- `tools/tui-ts/` : client terminal TypeScript (Bun + SolidJS + @opentui/core) — TUI avec streaming SSE natif. Lancer avec `bun run src/main.tsx`.

---

## Architecture

### Vue simplifiée

```
Utilisateur
    │
    ▼
┌─────────────┐     ┌─────────────────────────────────────────────────────────┐
│  AIGUILLEUR │     │                     PIPELINE                            │
│  (canal)    │────▶│  PORTAIL ──▶ SENTINELLE ──▶ ATELIER ──▶ SOUVENIR       │
│  Discord    │     │  (identité)   (ACL)       (LLM loop)  (mémoire SQLite)  │
│  WhatsApp   │◀────│                  │                                       │
│  REST       │     │                  └──▶ COMMANDANT (slash commands)       │
└─────────────┘     └─────────────────────────────────────────────────────────┘
                                            │
                                            ▼ relais:skill:trace
                                      ┌──────────────┐
                                      │   FORGERON   │◀── relais:memory:request
                                      │  - changelog │
                                      │  - skill auto│
                                      │  - correction│──▶ skill-designer
                                      └──────────────┘
```

Chaque flèche correspond à un **Redis Stream**. Chaque brique est un processus Python indépendant. La communication est asynchrone et résiliente : les messages non acquittés (`XACK`) restent dans la PEL (Pending Entry List) et sont redélivrés automatiquement.

### Flux réel

```mermaid
flowchart TD
    USERS([Utilisateurs externes])
  AIG["AIGUILLEUR<br/>adaptateur de canal"]
  PORTAIL["PORTAIL<br/>valide Envelope<br/>résout UserRegistry<br/>stamp user_record + user_id + llm_profile"]
    PENDING[(relais:admin:pending_users)]
  SENT_IN["SENTINELLE entrant<br/>ACL + routage"]
  ATELIER["ATELIER<br/>DeepAgents LangGraph"]
  COMMANDANT["COMMANDANT<br/>slash commands hors LLM"]
  SOUVENIR["SOUVENIR<br/>archive SQLite + fichiers agent"]
  SENT_OUT["SENTINELLE sortant<br/>pass-through actuel"]
    OUT["relais:messages:outgoing:{channel}"]
    STREAM["relais:messages:streaming:{channel}:{correlation_id}"]
    MEM_REQ[relais:memory:request]
    MEM_RES[relais:memory:response]
    DLQ[(relais:tasks:failed)]
    ARCHIVISTE["ARCHIVISTE<br/>observe logs events<br/>et une partie du pipeline"]
    FORGERON["FORGERON<br/>changelog + consolidation skills (S3)<br/>+ création auto skills"]
    SKILL_TRACE[relais:skill:trace]
    EVENTS_SYS[relais:events:system]

    USERS --> AIG
    AIG -->|"relais:messages:incoming"| PORTAIL

    PORTAIL -->|"relais:security"| SENT_IN
    PORTAIL -.->|"unknown_user_policy = pending"| PENDING

    SENT_IN -->|"message autorise<br/>relais:tasks"| ATELIER
    SENT_IN -->|"commande connue + ACL OK<br/>relais:commands"| COMMANDANT
    SENT_IN -->|"commande inconnue ou refusee<br/>reply inline"| OUT

    ATELIER -->|"relais:messages:streaming:{channel}:{correlation_id}"| STREAM
    ATELIER -->|"relais:messages:outgoing_pending<br/>reponse finale"| SENT_OUT
    ATELIER -->|"relais:messages:outgoing:{channel}<br/>progress events"| OUT
    ATELIER -->|"relais:memory:request<br/>archive action"| MEM_REQ
    ATELIER -->|"relais:tasks:failed"| DLQ

    COMMANDANT -->|"help -> outgoing:{channel}"| OUT
    COMMANDANT -->|"clear -> memory:request"| MEM_REQ
    MEM_REQ --> SOUVENIR

    SENT_OUT -->|"relais:messages:outgoing:{channel}"| OUT
    OUT --> AIG
    AIG --> USERS

    ATELIER -->|"relais:skill:trace"| SKILL_TRACE
    SKILL_TRACE --> FORGERON
    MEM_REQ -->|"forgeron_archive_group<br/>intent labeling + skill creation"| FORGERON
    FORGERON -->|"relais:events:system<br/>skill.created"| EVENTS_SYS
    FORGERON -->|"relais:messages:outgoing_pending<br/>notifications utilisateur"| SENT_OUT

    PORTAIL -. logs .-> ARCHIVISTE
    SENT_IN -. logs .-> ARCHIVISTE
    ATELIER -. logs .-> ARCHIVISTE
    COMMANDANT -. logs .-> ARCHIVISTE
    SOUVENIR -. logs .-> ARCHIVISTE
    FORGERON -. logs .-> ARCHIVISTE
    OUT -. pipeline observe partiel .-> ARCHIVISTE
    EVENTS_SYS -. events .-> ARCHIVISTE
```

### Streams Redis importants

| Stream | Producteur | Consommateur |
|--------|------------|--------------|
| `relais:messages:incoming` | Aiguilleur | Portail |
| `relais:messages:incoming:horloger` | Horloger | Portail |
| `relais:security` | Portail | Sentinelle |
| `relais:tasks` | Sentinelle | Atelier |
| `relais:commands` | Sentinelle | Commandant |
| `relais:commandant:query` | adaptateur REST | Commandant (`commandant_catalog_group`) |
| `relais:memory:request` | Atelier, Commandant | Souvenir (`souvenir_group`), Forgeron (`forgeron_archive_group`) |
| `relais:messages:outgoing_pending` | Atelier | Sentinelle |
| `relais:messages:outgoing:{channel}` | Sentinelle, Atelier, Commandant | Aiguilleur |
| `relais:messages:streaming:{channel}:{correlation_id}` | Atelier | adaptateur de canal streaming |
| `relais:tasks:failed` | Atelier | observateurs / diagnostics |
| `relais:admin:pending_users` | Portail | revue manuelle |
| `relais:skill:trace` | Atelier | Forgeron |
| `relais:memory:response:{correlation_id}` | Souvenir | Forgeron (BRPOP) |
| `relais:events:system` | Forgeron | Archiviste |
| `relais:logs` | toutes les briques | Archiviste |

### Comportement des briques

- `Portail` consomme `relais:messages:incoming`, résout l'utilisateur via `UserRegistry`, écrit `context["portail"]["user_record"]`, `context["portail"]["user_id"]` et `context["portail"]["llm_profile"]` (depuis `channel_profile` ou `"default"`), puis publie sur `relais:security`.
- `Sentinelle` consomme `relais:security`, applique les ACL, route les messages normaux vers `relais:tasks` et les slash commands vers `relais:commands`. Les commandes inconnues ou non autorisées génèrent une réponse inline directe sur `relais:messages:outgoing:{channel}`.
- `Commandant` consomme deux streams : `relais:commands` (dispatch des slash commandes — `/help`, `/clear`, `/sessions`, `/resume`) et `relais:commandant:query` (côté lecture CQRS — répond aux requêtes de catalogue du REST adapter via `LPUSH` sur une clé par requête `relais:commandant:catalog:{correlation_id}`, TTL 7 s). L'endpoint catalogue (`GET /v1/commands`) utilise ce flux CQRS pour l'auto-complétion TUI sans passer par le pipeline de tâches complet.
- `Atelier` consomme `relais:tasks`, gère l'historique conversationnel via le checkpointer LangGraph persistant (`AsyncSqliteSaver`, `checkpoints.db`, keyed by `user_id`), publie éventuellement le streaming sur `relais:messages:streaming:{channel}:{correlation_id}`, les événements de progression sur `relais:messages:outgoing:{channel}`, puis la réponse finale sur `relais:messages:outgoing_pending`. Atelier supporte une architecture 2-tier de sous-agents : sous-agents utilisateur dans `$RELAIS_HOME/config/atelier/subagents/<nom>/` (répertoire par sous-agent, avec `subagent.yaml`, `tools/*.py` optionnels), et sous-agents natifs dans `atelier/subagents/<nom>/` (livrés avec le code source). Sous-agents natifs livrés : `relais-config` (configuration CRUD canaux/profils), `horloger-manager` (CRUD des fichiers YAML de jobs Horloger, accessible via `/horloger` ou `/schedule`), `general-purpose` (remplace le worker built-in de deepagents pour imposer un contrat strict sans sortie visible par l'utilisateur ; hérite de tous les outils MCP du parent) et `skill-designer` (création interactive de SKILL.md depuis une correction utilisateur). L'accès par rôle est contrôlé via `allowed_subagents` dans `portail.yaml` (fnmatch patterns). Hot-reload supporté pour les modifications en temps réel.
- `Souvenir` consomme `relais:memory:request` (actions : `archive`, `clear`, `file_write`, `file_read`, `file_list`, `sessions`, `resume`). L'action `archive` est publiée par Atelier avec le contenu complet du tour et les `messages_raw` pour archivage dans SQLite. Les actions `sessions` et `resume` retournent les données à l'utilisateur via `relais:messages:outgoing:{channel}` (avec ownership enforcement). Les actions de fichier sont déclenchées par les agents via `SouvenirBackend`. L'historique court terme est géré par le checkpointer LangGraph d'Atelier (keyed par `user_id:session_id`).
- `Archiviste` observe `relais:logs`, `relais:events:*` et une liste partielle de streams pipeline. Il n'observe pas littéralement tous les streams.
- `Forgeron` dispose de trois pipelines autonomes :

  **Pipeline 1 — Amélioration des skills existants** (réécriture directe)

  Consomme `relais:skill:trace` (`forgeron_group`). Pour chaque trace, Forgeron évalue quatre conditions de déclenchement par skill. L'analyse se déclenche dès qu'**au moins une** est vraie (et que `edit_mode` est activé) :

  | Condition | Seuil | Ce qui est capturé |
  |-----------|-------|--------------------|
  | **Erreurs d'outils** | `tool_error_count >= edit_min_tool_errors` (défaut 1) | Turns où l'agent a échoué |
  | **Turn avorté (DLQ)** | `tool_error_count == -1` | Turns avortés par `ToolErrorGuard` — `messages_raw` contient la conversation partielle |
  | **Success after failure** | Turn courant 0 erreurs, turn précédent (même skill) avait des erreurs | Le "turn de correction" — là où l'agent a trouvé la bonne approche |
  | **Seuil d'usage** | `edit_call_threshold` appels cumulés (défaut 10) | Patterns d'utilisation normaux, même sans erreur |

  Un **cooldown Redis** par skill (`edit_cooldown_seconds`, défaut 300 s) empêche le spam d'éditions.

  `SkillEditor` reçoit le SKILL.md actuel + la trace de conversation scopée au skill cible (`scope_messages_to_skill`), appelle le LLM une seule fois (`edit_profile`, défaut `precise`) avec `with_structured_output`, et réécrit directement le SKILL.md si `changed=True`. Aucun fichier intermédiaire (changelog) n'est produit.

  **Pipeline 2 — Création automatique de skills**

  Consomme `relais:memory:request` (`forgeron_archive_group`, fan-out indépendant de Souvenir). Pour chaque session archivée :
  1. `IntentLabeler` (LLM rapide) extrait un label d'intention normalisé (ex: `"send_email"`). Si aucun pattern clair → arrêt.
  2. La session est enregistrée en SQLite avec son label.
  3. Quand `min_sessions_for_creation` sessions (défaut 3) partagent le même label ET que le cooldown de création a expiré (défaut 24h) :
     - `SkillCreator` (LLM precise) génère un `SKILL.md` complet à partir des sessions représentatives.
     - L'événement `skill.created` est publié sur `relais:events:system`.
     - Notification optionnelle à l'utilisateur via `relais:messages:outgoing_pending`.

  **Pipeline 3 — Correction utilisateur → `skill-designer`**

  Consomme également `relais:memory:request` (`forgeron_archive_group`). L'`IntentLabeler` peut détecter qu'une session est une **correction explicite** de l'utilisateur (`is_correction=True`) plutôt qu'un usage normal. Dans ce cas (et si `correction_mode` est activé) :
  1. Forgeron récupère l'historique complet de la session depuis Souvenir via `relais:memory:response:{correlation_id}` (BRPOP).
  2. Une notification est envoyée à l'utilisateur pour l'informer qu'une correction a été détectée.
  3. Un message `ACTION_MESSAGE_TASK` est publié sur `relais:tasks` avec `force_subagent = "skill-designer"` dans le contexte. Atelier délègue alors directement à `skill-designer`, qui engage un dialogue avec l'utilisateur pour créer un `SKILL.md` adapté.

---

## Commandes disponibles

Les commandes slash sont traitées hors LLM par le **Commandant**. Elles démarrent toutes par `/` et sont routées avant d'atteindre l'Atelier.

| Commande | Description |
|----------|-------------|
| `/help` | Affiche la liste des commandes disponibles. |
| `/clear` | Efface l'historique de la session courante (Redis + SQLite). |
| `/sessions` | Liste vos sessions récentes avec leur identifiant. |
| `/resume <id>` | Reprend une session précédente et charge son historique. |
| `/bundle install <zip> \| uninstall <name> \| list` | Gère les bundles de skills/outils (installation depuis un ZIP, désinstallation, ou liste des bundles installés). |

**Contrôle d'accès** : les commandes autorisées par rôle sont déclarées dans `roles.*.actions` de `portail.yaml`. `["*"]` donne accès à toutes les commandes, `[]` en interdit l'accès.

```yaml
# portail.yaml
roles:
  admin:
    actions: ["*"]        # toutes les commandes
  user:
    actions: ["clear", "sessions", "resume"]
  guest:
    actions: []           # aucune commande autorisée
```

Les commandes inconnues ou non autorisées génèrent une réponse inline directe (sans passer par l'Atelier).

---

## Installation

### Prérequis

- Python `>=3.11`
- `uv`
- `supervisord` si vous voulez utiliser le lancement supervisé
- Serveur Redis (voir installation ci-dessous)

#### Installer Redis

RELAIS utilise un **socket Unix** Redis (`./.relais/redis.sock`). Windows natif ne supporte pas les sockets Unix — utilisez WSL2 ou Docker à la place.

**macOS**
```bash
brew install redis
# Optionnel : démarrer Redis comme service en arrière-plan
brew services start redis
```

**Linux — Debian / Ubuntu**
```bash
sudo apt update && sudo apt install redis-server
sudo systemctl enable --now redis-server
```

**Linux — RHEL / Fedora / CentOS**
```bash
sudo dnf install redis
sudo systemctl enable --now redis
```

**Linux — Arch**
```bash
sudo pacman -S redis
sudo systemctl enable --now redis
```

**Windows**
Redis ne dispose pas de build natif officiel pour Windows. Deux options supportées :

- **WSL2** (recommandé) : installez Ubuntu via WSL2, puis suivez les étapes Debian/Ubuntu ci-dessus.
- **Docker** :
  ```bash
  docker run -d --name relais-redis \
    -v "$PWD/.relais:/data" \
    redis redis-server /data/redis.conf
  ```
  Vérifiez que `config/redis.conf` est correctement monté et que le chemin du socket est accessible depuis l'hôte.

Une fois Redis installé, RELAIS le démarre automatiquement via `supervisord` en utilisant `config/redis.conf` — pas besoin de le démarrer manuellement si vous utilisez `./supervisor.sh`.

### Chemin recommandé

```bash
git clone <repo-url>
cd relais

uv sync

cp .env.example .env

python -c "from common.init import initialize_user_dir; initialize_user_dir()"
```

### Ce que fait l'initialisation

`initialize_user_dir()` crée `RELAIS_HOME` et y copie l'ensemble des templates déclarés dans `common/init.DEFAULT_FILES`, notamment :

- `config/config.yaml`
- `config/portail.yaml`, `config/sentinelle.yaml`
- `config/atelier.yaml`, `config/atelier/profiles.yaml`, `config/atelier/mcp_servers.yaml`
- `config/aiguilleur.yaml`
- `config/HEARTBEAT.md`
- les prompts livrés (`prompts/soul/SOUL.md`, channels, policies, roles, users)

Si `config/aiguilleur.yaml` est supprimé après coup, `load_channels_config()` loggue un WARNING et retombe sur un fallback Discord-only.

### `RELAIS_HOME`

Par défaut, `RELAIS_HOME` vaut `./.relais` à la racine du dépôt. Vous pouvez le surcharger avec la variable d'environnement `RELAIS_HOME`.

La configuration et les prompts sont lus depuis `RELAIS_HOME`. Les répertoires `skills`, `logs`, `media` et `storage` restent centrés sur `RELAIS_HOME`.

---

## Arborescence de travail

Après initialisation, l'arborescence utilisateur ressemble à ceci :

```text
<RELAIS_HOME>/
├── config/
│   ├── config.yaml
│   ├── portail.yaml
│   ├── sentinelle.yaml
│   ├── atelier.yaml
│   ├── aiguilleur.yaml
│   ├── HEARTBEAT.md
│   └── atelier/
│       ├── profiles.yaml
│       ├── mcp_servers.yaml
│       └── subagents/          ← sous-agents custom (vide par défaut)
├── bundles/                    ← bundles ZIP installés
├── prompts/
│   ├── soul/
│   │   ├── SOUL.md
│   │   └── variants/
│   ├── channels/
│   ├── policies/
│   ├── roles/
│   └── users/
├── skills/
├── media/
├── logs/
├── backup/
└── storage/
    ├── memory.db
    └── horloger.db
```

`audit.db` n'est pas une base actuellement gérée par le code. L'Archiviste écrit surtout dans `logs/events.jsonl` et dans les logs de processus.

---

## Configuration et rechargement à chaud

### Rechargement à chaud (hot-reload)

Toutes les briques supportent le rechargement de leur configuration sans redémarrage de la brique.

**Mécanisme:**
- Chaque brique surveille ses fichiers YAML de configuration via `watchfiles` (détection système de changements fichier)
- À chaque changement détecté, la configuration est rechargée et validée atomiquement
- En cas d'erreur YAML, la configuration précédente est préservée (fallback sûr)
- Les configurations rechargées sont archivées dans `~/.relais/config/backups/{brick}_{timestamp}.yaml` (max 5 versions par brique)
- Les opérateurs peuvent aussi déclencher le rechargement manuellement via Redis Pub/Sub en envoyant `"reload"` sur `relais:config:reload:{brick_name}`

**Fichiers surveillés par brique:**
- **Portail**: `config/portail.yaml` (utilisateurs, rôles, politiques)
- **Sentinelle**: `config/sentinelle.yaml` (ACL, groupes)
- **Atelier**: `config/atelier.yaml`, `config/atelier/profiles.yaml`, `config/atelier/mcp_servers.yaml`
- **Souvenir**: aucun fichier surveillé — pas de config rechargeable
- **Aiguilleur**: `config/aiguilleur.yaml` (définitions canaux)

**Cas d'usage:**
- Modification des ACL (Sentinelle) sans redémarrage
- Ajout/suppression de profils LLM (Atelier) en direct
- Changement de politique utilisateur (Portail)
- Activation/désactivation de canaux (Aiguilleur)

### `config/config.yaml`

Le runtime lit aujourd'hui surtout `llm.default_profile` dans ce fichier, via `common.config_loader.get_default_llm_profile()`.

Exemple minimal fidèle :

```yaml
llm:
  default_profile: default
```

Le template livré contient aussi des blocs `redis`, `logging`, `security` et `paths`, mais le chemin d'exécution actuel s'appuie principalement sur les variables d'environnement pour Redis et les chemins runtime.

### `config/portail.yaml`

Ce fichier pilote l'identité utilisateur et la politique des inconnus.

Points importants :

- `unknown_user_policy` : `deny`, `guest` ou `pending`
- `guest_role` : rôle utilisé si `unknown_user_policy=guest`
- `users.*.prompt_path`
- `roles.*.prompt_path`
- `roles.*.skills_dirs`
- `roles.*.allowed_mcp_tools`
- `roles.*.allowed_subagents`

Exemple réduit :

```yaml
unknown_user_policy: deny
guest_role: guest

users:
  usr_admin:
    display_name: "Administrateur"
    role: admin
    blocked: false
    prompt_path: null
    identifiers:
      discord:
        dm: "123456789012345678"
        server: null

roles:
  admin:
    actions: ["*"]
    skills_dirs: ["*"]
    allowed_mcp_tools: ["*"]
    allowed_subagents: ["*"]
    prompt_path: null
  guest:
    actions: []
    skills_dirs: []
    allowed_mcp_tools: []
    allowed_subagents: []
    prompt_path: null
```

### `config/sentinelle.yaml`

La Sentinelle ne résout pas l'identité. Elle lit `user_record` depuis l'enveloppe enrichie par le Portail et applique ses règles ACL.

Exemple :

```yaml
access_control:
  default_mode: allowlist
  channels: {}

groups: []
```

### `config/atelier.yaml`

Le fichier pilote la publication des événements vers le channel.

```yaml
display:
  enabled: true
  final_only: true
  detail_max_length: 100
  events:
    tool_call: true
    tool_result: true
    subagent_start: true
    thinking: false
```

### `config/atelier/profiles.yaml`

Le loader lit ces champs :

- `model`
- `temperature`
- `max_tokens`
- `base_url`
- `api_key_env`
- `fallback_model`
- `max_turns`
- `mcp_timeout`
- `mcp_max_tools`
- `resilience.retry_attempts`
- `resilience.retry_delays`
- `resilience.fallback_model`

Exemple minimal :

```yaml
profiles:
  default:
    model: anthropic:claude-haiku-4-5
    temperature: 0.7
    max_tokens: 1024
    base_url: null
    api_key_env: ANTHROPIC_API_KEY
    fallback_model: null
    max_turns: 20
    mcp_timeout: 10
    mcp_max_tools: 20
    resilience:
      retry_attempts: 3
      retry_delays: [2, 5, 15]
      fallback_model: null
```

`base_url` peut utiliser une interpolation `${VAR}`. Si la variable n'existe pas au chargement, `load_profiles()` échoue immédiatement.

### `config/atelier/mcp_servers.yaml`

Le loader actuel lit les sections `mcp_servers.global` et `mcp_servers.contextual`, avec les entrées `enabled`, `type`, `command`, `args`, `url`, `env`, `profiles`.

Exemple :

```yaml
mcp_servers:
  global:
    - name: filesystem
      enabled: true
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]

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

### `config/aiguilleur.yaml`

`load_channels_config()` charge ce fichier via la cascade de config. Le template est copié par `initialize_user_dir()`. S'il est supprimé manuellement après coup, un WARNING est loggué et le code retombe sur un fallback minimal Discord-only.

Exemple :

```yaml
channels:
  discord:
    enabled: true
    streaming: false

  telegram:
    enabled: false
    streaming: true
    profile: fast

  whatsapp:
    enabled: false          # activer dans ~/.relais/config/aiguilleur.yaml
    streaming: false        # baileys-api ne supporte pas le streaming en MVP
    profile: default
    prompt_path: "channels/whatsapp_default.md"
    max_restarts: 5
```

Points importants :

- `streaming` est lu par chaque adaptateur et estampillé dans `context.aiguilleur["streaming"]` ; l'Atelier lit cette valeur par message (pas de cache au démarrage)
- `profile` force un profil LLM pour tout message du canal
- `prompt_path` force un overlay de prompt de canal (Layer 4)
- `type: external`, `command`, `args`, `class_path` et `max_restarts` sont pris en charge par le superviseur d'adaptateurs

> L'installation et la configuration du canal WhatsApp (install de la passerelle baileys-api, création de la clé API, pairing QR) sont prises en charge de bout en bout par le sous-agent `relais-config` via les skills `channel-setup` et `whatsapp`. Voir [docs/WHATSAPP_SETUP.md](docs/WHATSAPP_SETUP.md) pour le guide pas-à-pas manuel.

### `config/horloger.yaml`

Contrôle le planificateur CRON. Les specs de jobs sont stockées sous forme de fichiers YAML individuels dans `jobs_dir` (un fichier par job).

```yaml
tick_interval_seconds: 30       # fréquence de vérification des jobs à déclencher
catch_up_window_seconds: 120    # jobs plus anciens que cette fenêtre sont ignorés (pas de déclenchement en masse) après un downtime
jobs_dir: "config/horloger/jobs"
db_path: "storage/horloger.db"
```

Les jobs sont gérés via le sous-agent natif `horloger-manager` (accessible via `/horloger` ou `/schedule`) ou en créant manuellement des fichiers YAML dans `jobs_dir`.

---

## Prompts

Le prompt système est assemblé par `atelier.soul_assembler.assemble_system_prompt()` en 4 couches, dans cet ordre :

| # | Source | Chemin configuré dans |
|---|--------|-----------------------|
| 1 | `prompts/soul/SOUL.md` | toujours chargé |
| 2 | overlay de rôle | `roles[*].prompt_path` dans `portail.yaml` |
| 3 | override utilisateur | `users[*].prompt_path` dans `portail.yaml` |
| 4 | overlay de canal | `channels[*].prompt_path` dans `aiguilleur.yaml` |

Tous les chemins sont **explicites** (lus depuis la configuration YAML, pas inférés par convention). Les fichiers manquants ou vides sont silencieusement ignorés. Les couches présentes sont jointes avec `\n\n---\n\n`.

Comment ces chemins transitent dans le pipeline :
- Portail lit `portail.yaml`, résout le `UserRecord` et y inclut `role_prompt_path` (depuis `roles[*].prompt_path`) et `prompt_path` (depuis `users[*].prompt_path`).
- Aiguilleur stampe `channel_prompt_path` dans `context["aiguilleur"]` depuis `aiguilleur.yaml`.
- Atelier lit ces trois champs à chaque tour pour appeler `assemble_system_prompt()`.

Les fichiers `prompts/policies/*.md` (ex : `vacation.md`, `in_meeting.md`) sont livrés dans le dépôt mais **ne sont pas assemblés automatiquement** dans le prompt — ils existent comme templates à référencer manuellement dans un `prompt_path` de rôle ou d'utilisateur.

---

## Variables d'environnement

Les variables utiles au runtime actuel sont détaillées dans [docs/ENV.md](docs/ENV.md). Les plus importantes :

- `RELAIS_HOME`
- `REDIS_SOCKET_PATH`
- `REDIS_PASSWORD`
- `REDIS_PASS_AIGUILLEUR`
- `REDIS_PASS_PORTAIL`
- `REDIS_PASS_SENTINELLE`
- `REDIS_PASS_ATELIER`
- `REDIS_PASS_SOUVENIR`
- `REDIS_PASS_COMMANDANT`
- `REDIS_PASS_ARCHIVISTE`
- `REDIS_PASS_FORGERON`
- `ANTHROPIC_API_KEY`
- `OPENROUTER_API_KEY`
- `RELAIS_DB_PATH`
- `DISCORD_BOT_TOKEN`
- Canal WhatsApp (optionnel) : `WHATSAPP_GATEWAY_URL`, `WHATSAPP_PHONE_NUMBER`, `WHATSAPP_API_KEY`, `WHATSAPP_WEBHOOK_SECRET`, `WHATSAPP_WEBHOOK_PORT`, `WHATSAPP_WEBHOOK_HOST`, `REDIS_PASS_BAILEYS`

Pour les exemples MCP livrés dans les templates, `GITHUB_TOKEN` et `BRAVE_API_KEY` peuvent aussi être nécessaires selon les serveurs activés.

Pour activer WhatsApp, installez aussi les dépendances optionnelles : `uv sync --extra whatsapp` (ajoute `aiohttp>=3.9` et `qrcode>=7.0`).

---

## Démarrage

### Option recommandée : supervisord

Le chemin le plus complet du dépôt est le couple `supervisord.conf` + `supervisor.sh`.

```bash
./supervisor.sh start all
./supervisor.sh [--verbose] start all
./supervisor.sh [--verbose] restart all
./supervisor.sh status
./supervisor.sh stop all
./supervisor.sh reload all
```

**Flag `--verbose`** : Après démarrage/redémarrage, suit les logs de toutes les briques en temps réel. Appuyez sur `Ctrl+C` pour détacher les logs sans arrêter supervisord.

Le wrapper :

- démarre `supervisord` si nécessaire
- lance Redis local via `config/redis.conf` (socket Unix + port TCP `127.0.0.1:6379` pour les services annexes)
- démarre les briques des groupes `infra`, `core` et `relays` : `portail`, `sentinelle`, `atelier`, `souvenir`, `forgeron`, `commandant`, `archiviste`, `aiguilleur`
- ne démarre **pas** automatiquement le groupe `optional` (qui contient la passerelle `baileys-api` pour WhatsApp). L'installation/activation du canal WhatsApp est pilotée par le sous-agent `relais-config`.

### Démarrage manuel

```bash
# Terminal 1
redis-server config/redis.conf

# Terminals suivants
uv run python portail/main.py
uv run python sentinelle/main.py
uv run python atelier/main.py
uv run python souvenir/main.py
uv run python forgeron/main.py
uv run python commandant/main.py
uv run python archiviste/main.py
uv run python aiguilleur/main.py
```

L'entrée Aiguilleur est [aiguilleur/main.py](aiguilleur/main.py), pas un `main.py` séparé par canal. L'adaptateur Discord actuellement implémenté vit dans [aiguilleur/channels/discord/adapter.py](aiguilleur/channels/discord/adapter.py).

### Note Redis locale

Le dépôt démarre Redis avec [config/redis.conf](config/redis.conf), qui crée un socket Unix `./.relais/redis.sock` et des ACL par brique. Les mots de passe utilisés par les briques via `.env` doivent rester alignés avec cette configuration locale.

---

## Vérification rapide

```bash
redis-cli -s ./.relais/redis.sock XLEN relais:messages:incoming
redis-cli -s ./.relais/redis.sock XLEN relais:security
redis-cli -s ./.relais/redis.sock XLEN relais:tasks
redis-cli -s ./.relais/redis.sock XRANGE relais:tasks - +
```

Logs utiles :

- `./.relais/logs/events.jsonl`
- `./.relais/logs/*.log`

---

## Debug

Toutes les briques Python passent par [launcher.py](launcher.py) quand elles sont lancées via `supervisord.conf`. Le wrapper supporte :

- `DEBUGPY_ENABLED`
- `DEBUGPY_PORT`
- `DEBUGPY_WAIT`

Les ports configurés dans `supervisord.conf` sont :

| Brique | Port |
|--------|------|
| `atelier` | `5678` |
| `portail` | `5679` |
| `sentinelle` | `5680` |
| `archiviste` | `5681` |
| `souvenir` | `5682` |
| `commandant` | `5683` |
| `aiguilleur` | `5684` |
| `forgeron` | `5685` |

---

## Tests

```bash
pytest tests/ -v
```

Tests particulièrement utiles pour vérifier les affirmations structurelles :

- `tests/test_smoke_e2e.py`
- `tests/test_commandant_new_stream.py`
- `tests/test_channel_config.py`
- `tests/test_soul_assembler.py`

---

## Documentation liée

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) : référence technique par brique et par stream
- [docs/ENV.md](docs/ENV.md) : variables d'environnement réellement utiles
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) : workflow de contribution
