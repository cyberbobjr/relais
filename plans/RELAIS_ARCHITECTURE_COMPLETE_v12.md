# RELAIS — Document d'Architecture Complet
## Version 12 — Référence définitive

> **RELAIS** — *Station de relais : reçoit des messages de toutes origines,*
> *les achemine vers leur destination avec fiabilité et continuité.*
>
> Framework d'agents conversationnels multi-canaux, autonomes, extensibles,
> et auto-apprenants. Projet francophone, code anglais.

---

## Changements v12

- **Répertoire utilisateur `~/.relais/`** — config, skills, logs, médias stockés dans le home du compte qui lance RELAIS
- **Cascade de résolution** — `~/.relais/` surcharge `/opt/relais/` surcharge `./`
- **Variable `RELAIS_HOME`** — override explicite du répertoire utilisateur
- **Initialisation au premier lancement** — création automatique de `~/.relais/` avec les fichiers par défaut

### Phase 5 — InternalTools + MCP stdio

- **`InternalTool`** — frozen dataclass dans `atelier/internal_tool.py` : `name`, `description`, `input_schema`, `handler` (callable sync ou async)
- **`make_skills_tools(skills_dir)`** — construit deux outils internes : `list_skills` (catalogue) et `read_skill` (lecture complète d'un `SKILL.md`)
- **`load_for_sdk(profile)`** — lit `mcp_servers.yaml`, filtre par `enabled` et profil, retourne un `dict[str, {command, args, env}]` prêt pour `SDKExecutor`
- **`SDKExecutor`** — démarre les serveurs MCP via `AsyncExitStack` + `mcp.client.stdio.stdio_client`, préfixe les outils MCP `{server}__{tool}`, gère la boucle agentique explicite (`stop_reason == "tool_use"` → injection `tool_result` → rebouclage)

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
11. L'Atelier — exécution des agents, résilience LLM, sous-agents
12. Le Souvenir — mémoire, compaction, pagination
13. Le Veilleur — planification, backup, rétention
14. Le Forgeron — auto-apprentissage & versioning skills
15. L'Archiviste — logs & audit
16. Le Crieur — push proactif & multi-canal
17. Le Guichet — webhooks entrants
18. Le Vigile — administration NLP & hot reload
19. Le Tableau — TUI bidirectionnel
20. Le Tisserand — extensions intercepteurs
21. Le Scrutateur — monitoring
22. SOUL.md — personnalité JARVIS & i18n
23. Profils — modélisation complète
24. Politique de réponse automatique
25. Gestion des médias
26. Système d'extensions
27. Sécurité
28. Corrélation end-to-end
29. Structure du projet
30. La Charte RELAIS v12

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

```python
# common/config_loader.py

def get_relais_home() -> Path:
    """
    Returns the RELAIS user directory.
    Override via RELAIS_HOME environment variable.
    """
    custom = os.environ.get("RELAIS_HOME")
    if custom:
        return Path(custom)
    return Path.home() / ".relais"


# Search path — user config always takes priority
CONFIG_SEARCH_PATH = [
    get_relais_home(),          # 1. ~/.relais/      (user — highest priority)
    Path("/opt/relais"),        # 2. /opt/relais/    (system installation)
    Path("./"),                 # 3. ./              (current dir — dev mode)
]


def resolve_config_path(filename: str) -> Path:
    """
    Resolves a config file using cascade priority.
    User config in ~/.relais/ always overrides system config.

    Example:
      ~/.relais/config/config.yaml      → found → use it
      ~/.relais/config/profiles.yaml    → not found → try /opt/relais/
      /opt/relais/config/profiles.yaml  → found → use it
    """
    for base in CONFIG_SEARCH_PATH:
        candidate = base / filename
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"Config file '{filename}' not found.\n"
        f"Searched: {[str(p / filename) for p in CONFIG_SEARCH_PATH]}"
    )


def resolve_skills_dir() -> Path:
    """
    Skills directory is ALWAYS in user home — never system.
    Le Forgeron writes here. CLAUDE.md references paths here.
    """
    path = get_relais_home() / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_logs_dir() -> Path:
    """L'Archiviste always writes to user home logs."""
    path = get_relais_home() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_media_dir() -> Path:
    """Temporary media files — always in user home."""
    path = get_relais_home() / "media"
    path.mkdir(parents=True, exist_ok=True)
    return path
```

### Initialisation au premier lancement

```python
# common/init.py
import shutil

SYSTEM_INSTALL_PATH = Path("/opt/relais")

# Default template files shipped with the system installation
DEFAULT_FILES = [
    ("config/config.yaml",          "config/config.yaml.default"),
    ("config/profiles.yaml",        "config/profiles.yaml.default"),
    ("config/users.yaml",           "config/users.yaml.default"),
    ("config/reply_policy.yaml",    "config/reply_policy.yaml.default"),
    ("config/mcp_servers.yaml",     "config/mcp_servers.yaml.default"),
    ("config/HEARTBEAT.md",         "config/HEARTBEAT.md.default"),
    ("soul/SOUL.md",                "soul/SOUL.md.default"),
    ("soul/variants/SOUL_concise.md",       "soul/variants/SOUL_concise.md.default"),
    ("soul/variants/SOUL_professional.md",  "soul/variants/SOUL_professional.md.default"),
]


def initialize_user_dir():
    """
    Creates ~/.relais/ structure on first run.
    Copies default templates from system installation.
    NEVER overwrites existing user files — safe to call on every startup.
    """
    home = get_relais_home()

    # Create directory structure
    dirs = [
        "config", "soul/variants", "prompts",
        "skills/manual", "skills/auto",
        "media", "logs", "backup"
    ]
    for d in dirs:
        (home / d).mkdir(parents=True, exist_ok=True)

    # Copy defaults — only if file doesn't exist yet
    for dest_rel, src_rel in DEFAULT_FILES:
        dest = home / dest_rel
        src = SYSTEM_INSTALL_PATH / src_rel
        if not dest.exists() and src.exists():
            shutil.copy(src, dest)

    # Create empty CLAUDE.md for skills registry if not present
    claude_md = home / "skills" / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(
            "# RELAIS Skills Registry\n\n"
            "## Skills actifs\n"
            "# Ajoutez vos skills ici — Le Forgeron met à jour automatiquement\n"
        )
```

### Variable d'environnement

```bash
# .env — override du répertoire utilisateur
RELAIS_HOME=/custom/path/relais     # optionnel — défaut : ~/.relais

# Exemples d'usage
RELAIS_HOME=/srv/relais             # serveur multi-utilisateurs
RELAIS_HOME=/tmp/relais-test        # tests d'intégration
```

### Impact sur les briques

| Brique | Ce qui change |
|---|---|
| L'Archiviste | Écrit dans `~/.relais/logs/` |
| Le Forgeron | Lit/écrit dans `~/.relais/skills/auto/` |
| L'Atelier | Charge les skills depuis `~/.relais/skills/` |
| Le Souvenir | DB dans `~/.relais/storage/memory.db` via `resolve_storage_dir()` |
| Le Portail | Charge `~/.relais/config/reply_policy.yaml` |
| Le Vigile | Charge `~/.relais/soul/SOUL.md` pour hot reload |
| Le Veilleur | Lit `~/.relais/config/HEARTBEAT.md` + backup vers `~/.relais/backup/` |
| Tous | Config chargée via `resolve_config_path()` — cascade automatique |

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
│                      │ → Le Guichet                             │
├──────────────────────┼──────────────────────────────────────────┤
│ STREAM CONSUMER      │ Consomme Stream, exécute, répond         │
│                      │ → L'Atelier, Le Souvenir                 │
├──────────────────────┼──────────────────────────────────────────┤
│ RELAY                │ Canal externe ↔ Redis                    │
│                      │ → L'Aiguilleur (N instances)             │
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

| Brique | Module | Port | Taxonomie | Rôle |
|---|---|---|---|---|
| 🚦 **L'Aiguilleur** | `aiguilleur/` | 810x | Relay | Adaptateur de canaux — 1 instance/canal |
| 🏛️ **Le Portail** | `portail/` | 8000 | Transformer | Routage, identification, politique |
| 🛡️ **La Sentinelle** | `sentinelle/` | 8001 | Transformer | ACL, profils, guardrails |
| 📨 **Le Coursier** | Redis | — | Infrastructure | Bus messages Unix socket |
| ⚒️ **L'Atelier** | `atelier/` | 8002 | Stream Consumer | Exécution agents LLM |
| 💭 **Le Souvenir** | `souvenir/` | 8003 | Stream Consumer | Mémoire contexte + longue durée |
| 🌙 **Le Veilleur** | `veilleur/` | 8004 | Pure Publisher | CRON + Heartbeat + backup |
| 🔧 **Le Forgeron** | `forgeron/` | 8005 | Batch Processor | Génération skills auto |
| 📚 **L'Archiviste** | `archiviste/` | 8006 | Pure Observer | Logs → JSONL + SQLite |
| 📣 **Le Crieur** | `crieur/` | 8007 | Transformer | Push proactif multi-canal |
| 🔗 **Le Guichet** | `guichet/` | 8008 | Transformer | Webhooks HMAC → pipeline |
| 🔱 **Le Vigile** | `vigile/` | 8009 | Admin | NLP → supervisord + hot reload |
| 📊 **Le Tableau** | `tableau/` | 8010 | Admin + Relay | TUI bidirectionnel |
| 🧵 **Le Tisserand** | `tisserand/` | — | Interceptor Chain | Extensions in-process |
| 🔍 **Le Scrutateur** | `scrutateur/` | 8011 | Pure Observer | Prometheus + Loki + ES |

---

## 6. Infrastructure — supervisord & MCP servers

### supervisord.conf — ordre de démarrage

```
priority 1   → Le Coursier (Redis)
priority 5   → LiteLLM proxy
priority 6   → MCP servers globaux (supervisord)
priority 8   → Observers purs (L'Archiviste, Le Scrutateur)
priority 10  → Briques core
priority 20  → Les instances de L'Aiguilleur
priority 30  → Le Tableau (local, à la demande)
```

### MCP servers lifecycle — modèle hybride

Deux types de MCP servers selon leur nature :

```
MCP GLOBAUX — supervisord (processus persistants)
  Toujours disponibles, légers, indépendants du contexte
  Démarrent avec RELAIS, vivent toute la durée de vie du système

  [program:mcp-calendar]
  [program:mcp-brave-search]

MCP CONTEXTUELS — claude-agent-sdk (lancés à la demande)
  Liés à un projet ou un contexte spécifique
  Spawned par L'Atelier pour chaque session, tués en fin de tâche
  Définis dans profiles.yaml sous mcp_servers

  Ex: mcp__jcodemunch, mcp__gitlab
```

### config/mcp_servers.yaml

Format canonique avec clé racine `mcp_servers:` — deux transports supportés : `stdio` (sous-processus spawné par l'Atelier) et `sse` (connexion à un serveur HTTP existant).

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

```ini
; supervisord.conf — MCP globaux (priority 6)
[program:mcp-calendar]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 mcp/calendar/server.py'
directory=/opt/relais
priority=6
autostart=true
autorestart=true
stdout_logfile=/var/log/relais/mcp-calendar.log

[program:mcp-brave-search]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec node mcp/brave-search/server.js'
directory=/opt/relais
priority=6
autostart=true
autorestart=true
stdout_logfile=/var/log/relais/mcp-brave-search.log
```

### supervisord.conf — complet

```ini
[supervisord]
logfile=/var/log/relais/supervisord.log
pidfile=/var/run/relais/supervisord.pid
nodaemon=false

[unix_http_server]
file=/var/run/relais/supervisor.sock
chmod=0700

[supervisorctl]
serverurl=unix:///var/run/relais/supervisor.sock

[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface

; priority 1 — infrastructure
[program:courier]
command=redis-server /opt/relais/config/redis.conf
priority=1
autostart=true
autorestart=true
stdout_logfile=/var/log/relais/courier.log

; priority 5 — LLM proxy
[program:litellm]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec litellm --config config/litellm.yaml --port 4000'
directory=/opt/relais
priority=5
autostart=true
autorestart=true
stdout_logfile=/var/log/relais/litellm.log

; priority 6 — MCP globaux
[program:mcp-calendar]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 mcp/calendar/server.py'
directory=/opt/relais
priority=6
autostart=true
autorestart=true
stdout_logfile=/var/log/relais/mcp-calendar.log

[program:mcp-brave-search]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec node mcp/brave-search/server.js'
directory=/opt/relais
priority=6
autostart=true
autorestart=true
stdout_logfile=/var/log/relais/mcp-brave-search.log

; priority 8 — pure observers
[program:archiviste]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 archiviste/main.py'
directory=/opt/relais
priority=8
autostart=true
autorestart=true
stopwaitsecs=10
stopsignal=TERM
stdout_logfile=/var/log/relais/archiviste.log

[program:scrutateur]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 scrutateur/main.py'
directory=/opt/relais
priority=8
autostart=true
autorestart=true
stopwaitsecs=10
stopsignal=TERM
stdout_logfile=/var/log/relais/scrutateur.log

; priority 10 — core bricks
[program:sentinelle]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 sentinelle/main.py'
directory=/opt/relais
priority=10
autostart=true
autorestart=true
startretries=10
stopwaitsecs=10
stopsignal=TERM
stdout_logfile=/var/log/relais/sentinelle.log

[program:souvenir]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 souvenir/main.py'
directory=/opt/relais
priority=10
autostart=true
autorestart=true
stopwaitsecs=15
stopsignal=TERM
stdout_logfile=/var/log/relais/souvenir.log

[program:atelier]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 atelier/main.py'
directory=/opt/relais
priority=10
autostart=true
autorestart=true
startretries=5
stopwaitsecs=35
stopsignal=TERM
stdout_logfile=/var/log/relais/atelier.log

[program:crieur]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 crieur/main.py'
directory=/opt/relais
priority=10
autostart=true
autorestart=true
stopwaitsecs=10
stopsignal=TERM
stdout_logfile=/var/log/relais/crieur.log

[program:portail]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 portail/main.py'
directory=/opt/relais
priority=10
autostart=true
autorestart=true
stopwaitsecs=15
stopsignal=TERM
stdout_logfile=/var/log/relais/portail.log

[program:vigile]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 vigile/main.py'
directory=/opt/relais
priority=10
autostart=true
autorestart=true
stopwaitsecs=10
stopsignal=TERM
stdout_logfile=/var/log/relais/vigile.log

[program:veilleur]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 veilleur/main.py'
directory=/opt/relais
priority=10
autostart=true
autorestart=true
stopwaitsecs=10
stopsignal=TERM
stdout_logfile=/var/log/relais/veilleur.log

[program:guichet]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 guichet/main.py'
directory=/opt/relais
priority=10
autostart=false
autorestart=true
stopwaitsecs=10
stopsignal=TERM
stdout_logfile=/var/log/relais/guichet.log

[program:forgeron]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 forgeron/main.py'
directory=/opt/relais
priority=20
autostart=false
autorestart=false
stdout_logfile=/var/log/relais/forgeron.log

; priority 20 — L'Aiguilleur instances
[program:aiguilleur-telegram]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 aiguilleur/telegram/main.py'
directory=/opt/relais
priority=20
autostart=true
autorestart=true
stopwaitsecs=10
stopsignal=TERM
stdout_logfile=/var/log/relais/aiguilleur-telegram.log

[program:aiguilleur-discord]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 aiguilleur/discord/main.py'
directory=/opt/relais
priority=20
autostart=true
autorestart=true
stopwaitsecs=10
stdout_logfile=/var/log/relais/aiguilleur-discord.log

[program:aiguilleur-slack]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 aiguilleur/slack/main.py'
directory=/opt/relais
priority=20
autostart=false
autorestart=true
stdout_logfile=/var/log/relais/aiguilleur-slack.log

[program:aiguilleur-matrix]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 aiguilleur/matrix/main.py'
directory=/opt/relais
priority=20
autostart=false
autorestart=true
stdout_logfile=/var/log/relais/aiguilleur-matrix.log

[program:aiguilleur-teams]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 aiguilleur/teams/main.py'
directory=/opt/relais
priority=20
autostart=false
autorestart=true
stdout_logfile=/var/log/relais/aiguilleur-teams.log

[program:aiguilleur-rest]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 aiguilleur/rest/main.py'
directory=/opt/relais
priority=20
autostart=true
autorestart=true
stopwaitsecs=10
stdout_logfile=/var/log/relais/aiguilleur-rest.log

[program:aiguilleur-whatsapp]
command=node aiguilleur/whatsapp/index.js
directory=/opt/relais
priority=20
autostart=true
autorestart=true
startretries=3
stopwaitsecs=15
stdout_logfile=/var/log/relais/aiguilleur-whatsapp.log

[program:aiguilleur-signal]
command=bash aiguilleur/signal/run.sh
directory=/opt/relais
priority=20
autostart=false
autorestart=true
stdout_logfile=/var/log/relais/aiguilleur-signal.log

; priority 30 — interfaces locales
[program:tableau]
command=bash -c 'set -a; source /opt/relais/.env; set +a; exec python3 tableau/main.py'
directory=/opt/relais
priority=30
autostart=false
autorestart=false
stdout_logfile=/var/log/relais/tableau.log

; groups
[group:mcp]
programs=mcp-calendar,mcp-brave-search

[group:observers]
programs=archiviste,scrutateur

[group:relays]
programs=aiguilleur-telegram,aiguilleur-discord,aiguilleur-rest,aiguilleur-whatsapp

[group:core]
programs=portail,sentinelle,atelier,souvenir,crieur,vigile,veilleur

[group:relais]
programs=mcp,observers,relays,core,litellm
```

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

user guichet    on >${REDIS_PASS_GUICHET}
  ~relais:push:* ~relais:webhooks:*
  +publish

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
  relais:tasks:failed                   Atelier → DLQ (SDKExecutionError exhausted)
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
  relais:webhooks:*              Guichet → Crieur/Atelier
  relais:media:*                 Aiguilleur → Portail (métadonnées médias)
```

---

## 8. L'Aiguilleur — adaptateur de canaux & formatage Markdown

### Deux responsabilités à la sortie

L'Aiguilleur fait la conversion Markdown à la **sortie uniquement** (réponses vers le canal). Chaque instance connaît les règles syntaxiques de son canal.

```python
# aiguilleur/base.py
class AiguilleurBase(ABC):

    @abstractmethod
    async def receive(self) -> Envelope: ...

    @abstractmethod
    async def send(self, envelope: Envelope) -> None: ...

    def format_for_channel(self, text: str) -> str:
        """
        Converts Markdown to channel-specific syntax.
        Override in each relay implementation.
        Default: passthrough (for channels supporting Markdown natively).
        """
        return text

    async def health(self) -> dict:
        return {"status": "ok", "brick": self.name, "channel": self.channel_name}
```

### Règles de formatage par canal

```python
# aiguilleur/telegram/main.py
def format_for_channel(self, text: str) -> str:
    """Telegram uses its own Markdown variant (MarkdownV2)."""
    # **bold** → *bold*
    # `code` → `code` (same)
    # [link](url) → [link](url) (same)
    # escape special chars: _ * [ ] ( ) ~ ` > # + - = | { } . !
    return convert_md_to_telegram(text)

# aiguilleur/whatsapp/main.py (ou index.js)
def format_for_channel(self, text: str) -> str:
    """WhatsApp does not render Markdown — strip all formatting."""
    return strip_markdown(text)

# aiguilleur/discord/main.py
def format_for_channel(self, text: str) -> str:
    """Discord renders standard Markdown natively."""
    return text  # passthrough

# aiguilleur/slack/main.py
def format_for_channel(self, text: str) -> str:
    """Slack uses mrkdwn syntax."""
    return convert_md_to_slack_mrkdwn(text)

# aiguilleur/tui/main.py
def format_for_channel(self, text: str) -> str:
    """Textual renders Markdown natively."""
    return text  # passthrough

# aiguilleur/rest/main.py
def format_for_channel(self, text: str) -> str:
    """REST returns raw Markdown — client handles rendering."""
    return text  # passthrough
```

### Authentification canal REST

```python
# aiguilleur/rest/main.py
from fastapi import FastAPI, Header, HTTPException
import os

app = FastAPI(
    title="RELAIS REST Relay",
    docs_url="/docs",      # Swagger UI — activé par défaut
    redoc_url="/redoc"     # ReDoc — activé par défaut
)

REST_API_KEY = os.environ.get("REST_API_KEY")

async def verify_api_key(x_api_key: str = Header(...)):
    """Simple static API Key middleware."""
    if x_api_key != REST_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

@app.post("/message", dependencies=[Depends(verify_api_key)])
async def receive_message(body: MessageRequest):
    """
    Send a message to RELAIS.
    Authentication: X-Api-Key header (static key from .env REST_API_KEY)
    """
    ...
```

### Configuration des canaux via `channels.yaml`

Chaque canal est configuré et activé/désactivé via le fichier `channels.yaml` (cascade de résolution : `~/.relais/config/` → `/opt/relais/config/` → `./config/`). Cette centralisation permet de gérer les adaptateurs sans modification du code.

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
- `enabled` — activé/désactivé (pas de suppression de fichiers, juste un toggle)
- `streaming` — supporte le streaming progressif (flag utilisé par Atelier pour `STREAMING_CAPABLE_CHANNELS`)
- `type` — `native` (thread Python + asyncio) ou `external` (subprocess via `Popen`)
- `class_path` — override de la classe adaptateur (convention : `aiguilleur.channels.{name}.adapter.{Name}Aiguilleur`)
- `max_restarts` — nombre max de redémarrages avant abandon (restart automatique avec backoff exponentiel `min(2^count, 30)` secondes)
- `command`/`args` — requis pour type `external` uniquement

**Découverte automatique des adaptateurs :**
- Adaptateurs natifs : convention `aiguilleur.channels.{channel_name}.adapter` si `type: native`
- Classe : `{ChannelName}Aiguilleur(NativeAiguilleur)` (ex. `DiscordAiguilleur`)
- Si `class_path` fourni : utilisé à la place

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

---

## 9. Le Portail — routage & politique de réponse

### Registre des sessions actives

```python
async def update_active_sessions(user_id: str, channel: str):
    """
    Updated on every incoming message.
    Le Crieur reads this hash to resolve notification targets.
    TTL: 1h (refreshed on each message)
    """
    key = f"relais:active_sessions:{user_id}"
    await redis.hset(key, channel, datetime.utcnow().timestamp())
    await redis.expire(key, 3600)
```

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
      normal: last_active     # notifie Benjamin sur son dernier canal actif
      high: all_active
      critical: all_active
    notification_target_user: usr_benjamin  # les réponses vont à Benjamin
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

**Updated 2026-03-30:** L'Atelier utilise désormais le SDK Python natif `anthropic` (AsyncAnthropic) avec une boucle agentique tool-use explicite. La dépendance `claude-agent-sdk` et le binaire CLI Node.js `claude` sont supprimés. Les serveurs MCP stdio restent supportés via le SDK Python `mcp`.

### Architecture générale

L'Atelier follows this flow for each incoming task:

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
Build InternalTool list (make_skills_tools)
  ↓
Execute via SDKExecutor (anthropic.AsyncAnthropic + explicit tool-use loop)
  ├─ base_url=ANTHROPIC_BASE_URL (LiteLLM proxy — direct, no CLI wrapper)
  ├─ Start MCP stdio servers via mcp Python SDK + AsyncExitStack
  ├─ Merge InternalTool + MCP tools → Anthropic tools list
  └─ Loop: stream → tool calls → results → next turn, until end_turn or max_turns
  ↓
If streaming capable (Discord/Telegram): publish chunks to relais:messages:streaming:{channel}:{correlation_id}
  ↓
Publish response to relais:messages:outgoing:{channel}
  ↓
Conditional XACK (success or DLQ) — never lose messages on retry
```

### Stack technique — remplacement de claude-agent-sdk

| Composant | Ancienne approche | Nouvelle approche |
|-----------|------------------|------------------|
| Appels LLM | `claude-agent-sdk` → spawn CLI `claude` | `anthropic.AsyncAnthropic` direct |
| `ANTHROPIC_BASE_URL` | Workaround Bug #677 (cli_path) | Supporté nativement via `base_url=` |
| Dépendance externe | Binaire Node.js `claude` obligatoire | Python pur, aucun binaire requis |
| Boucle tool-use | Gérée par le CLI (opaque) | Explicite dans `_run_agentic_loop()` |
| Serveurs MCP | Via `ClaudeAgentOptions.mcp_servers` | `mcp` Python SDK + `stdio_client` |
| Outils natifs | AgentDefinition (abandonné) | `InternalTool` avec handler Python |

**Dépendances pip :** `anthropic`, `mcp` (remplacent `claude-agent-sdk`)

### Modules — atelier/

```
atelier/
├── main.py            # Brique principale — loop Redis, dispatch
├── sdk_executor.py    # SDKExecutor : AsyncAnthropic + boucle agentique
├── internal_tool.py   # Dataclass InternalTool (outil natif Python)
├── skills_tools.py    # make_skills_tools() → list_skills + read_skill
├── mcp_loader.py      # Chargement config MCP servers (inchangé)
├── profile_loader.py  # ProfileConfig, ResilienceConfig (inchangé)
├── soul_assembler.py  # Assemblage prompt système (inchangé)
└── stream_publisher.py # Publication chunks Redis (inchangé)
```

### SDK integration — anthropic.AsyncAnthropic

```python
# atelier/sdk_executor.py
import anthropic
import contextlib

class SDKExecutor:
    def __init__(self, profile, soul_prompt, mcp_servers, tools=None):
        self._client = anthropic.AsyncAnthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY", ...),
            base_url=os.environ.get("ANTHROPIC_BASE_URL", "http://localhost:4000"),
        )
        self._internal_tools = {t.name: t for t in (tools or [])}

    async def execute(self, envelope, context, stream_callback=None) -> str:
        messages = self._build_messages(envelope, context)
        async with contextlib.AsyncExitStack() as stack:
            mcp_tools, mcp_sessions = await self._start_mcp_servers(stack)
            return await self._run_agentic_loop(messages, mcp_tools, mcp_sessions, stream_callback)
```

### Boucle agentique — _run_agentic_loop()

```python
async def _run_agentic_loop(self, messages, mcp_tools, mcp_sessions, stream_callback):
    all_tools = self._get_anthropic_tools(mcp_tools)  # internal + MCP
    full_reply = ""

    for turn in range(self._profile.max_turns):
        async with self._client.messages.stream(
            model=self._profile.model,
            max_tokens=self._profile.max_tokens,
            system=self._soul_prompt,
            messages=messages,
            tools=all_tools or None,
        ) as stream:
            async for text in stream.text_stream:
                full_reply += text
                if stream_callback:
                    await stream_callback(text)   # streaming temps réel Discord/Telegram
            final_msg = await stream.get_final_message()

        if final_msg.stop_reason != "tool_use":
            break  # end_turn, max_tokens, stop_sequence → fin

        # Construire le tour assistant (text + tool_use blocks)
        assistant_content = [{"type": b.type, ...} for b in final_msg.content]
        messages.append({"role": "assistant", "content": assistant_content})

        # Exécuter les outils et injecter les résultats
        tool_results = []
        for block in final_msg.content:
            if block.type == "tool_use":
                result = await self._call_tool(block.name, block.input, mcp_sessions)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

    return full_reply
```

### InternalTool — outils natifs Python

`InternalTool` (dataclass frozen dans `atelier/internal_tool.py`) expose des outils Python directement dans la boucle agentique, sans serveur MCP.

```python
@dataclass(frozen=True)
class InternalTool:
    name: str           # identifiant envoyé à l'API Anthropic
    description: str    # aide le modèle à choisir l'outil
    input_schema: dict  # JSON Schema {"type": "object", "properties": {...}}
    handler: Callable[..., str | Awaitable[str]]  # sync ou async
```

**Dispatch dans `_call_tool()` :**
1. Si `tool_name in self._internal_tools` → appel du handler Python (sync ou async)
2. Sinon → `_call_mcp_tool(tool_name, tool_input, mcp_sessions)` (format `server__tool`)

### Skills InternalTools — make_skills_tools()

`atelier/skills_tools.py` expose deux `InternalTool` pour la gestion des skills :

| Outil | Description |
|-------|-------------|
| `list_skills` | Scanne `skills_dir` récursivement pour les `SKILL.md`, retourne un catalogue `"- {nom}: {première ligne}"` |
| `read_skill(skill_name)` | Lit et retourne le contenu complet du `SKILL.md` correspondant |

```python
# atelier/main.py — dans _handle_message()
tools = make_skills_tools(self._skills_dir)
sdk_executor = SDKExecutor(profile=profile, ..., tools=tools)
```

`self._skills_dir` est chargé au démarrage depuis `Path(__file__).parent.parent / "skills"`.

### Serveurs MCP — _start_mcp_servers()

`SDKExecutor._start_mcp_servers()` prend en charge deux transports :

```python
async def _start_mcp_servers(self, stack: AsyncExitStack):
    for server_name, cfg in self._mcp_servers.items():
        transport = cfg.get("type", "stdio")

        if transport == "stdio":
            params = StdioServerParameters(
                command=cfg["command"], args=cfg.get("args", []), env=cfg.get("env") or None
            )
            read, write = await stack.enter_async_context(stdio_client(params))

        elif transport == "sse":
            # mcp.client.sse est optionnel — guard _SSE_AVAILABLE
            if not _SSE_AVAILABLE:
                logger.warning("sse_client non disponible, serveur '%s' ignoré", server_name)
                continue
            read, write = await stack.enter_async_context(sse_client(cfg["url"]))

        else:
            logger.warning("Transport inconnu '%s' pour '%s', ignoré", transport, server_name)
            continue

        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tool_list = await session.list_tools()
        # Préfixe {server_name}__{tool_name} pour éviter les collisions
        for tool in tool_list.tools:
            tools.append({"name": f"{server_name}__{tool.name}", ...})
        sessions[server_name] = session
    return tools, sessions
```

**Import conditionnel :** si le package `mcp` est absent, `_start_mcp_servers()` loggue un warning et retourne `([], {})` sans crash. `_SSE_AVAILABLE` est levé à `False` si `mcp.client.sse` est absent. Les InternalTools restent fonctionnels dans les deux cas.

**Format retourné par `mcp_loader.load_for_sdk()` :**
```python
# Serveur stdio
{"server_name": {"type": "stdio", "command": "...", "args": [...], "env": {...}}}

# Serveur SSE
{"server_name": {"type": "sse", "url": "http://...", "env": {...}}}
# env omis si vide
```

### ProfileConfig — champs clés

```python
# atelier/profile_loader.py
@dataclass(frozen=True)
class ProfileConfig:
    model: str
    temperature: float
    max_tokens: int              # ← Passé à AsyncAnthropic.messages.stream()
    resilience: ResilienceConfig
    max_turns: int = 20          # ← Nombre max de tours de la boucle agentique
    mcp_timeout: int = 10        # ← Timeout (s) par appel outil MCP (asyncio.wait_for)
    mcp_max_tools: int = 20      # ← Max outils MCP exposés au modèle (0 = aucun)
    # … autres champs : allowed_tools, allowed_mcp, guardrails, memory_scope, fallback_model
```

`max_turns` contrôle le nombre d'itérations de la boucle `_run_agentic_loop()`.

`mcp_timeout` est appliqué via `asyncio.wait_for(session.call_tool(...), timeout=profile.mcp_timeout)` dans `_call_mcp_tool()`. Un `TimeoutError` retourne une chaîne d'erreur au modèle sans interrompre la boucle.

`mcp_max_tools` tronque la liste des outils MCP dans `_get_anthropic_tools()` : `mcp_tools[:profile.mcp_max_tools]`. Les outils internes (`InternalTool`) ne sont pas comptés dans cette limite. La valeur `0` est utilisée par le profil `memory_extractor` pour désactiver complètement les outils MCP.

### Profil `memory_extractor` — extraction légère de faits utilisateur (Axe C)

**New in 2026-03-29:** A dedicated profile `memory_extractor` is used by Souvenir for identifying user facts.

Le Souvenir utilise ce profil pour extraire automatiquement les faits utilisateur des conversations. Ce profil est optimisé pour :
- **Modèle léger** : `glm-4.7-flash` (vs gpt-3.5-turbo précédemment hardcodé)
- **Latence minimale** : `max_tokens: 512`, `temperature: 0.1`
- **Pas de mémoire contexte** : `short_term_messages: 0`
- **Pas de streaming** : `stream: false`
- **Résilience légère** : 2 retries avec délai `[1, 3]`

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

**Chargement dynamique dans Souvenir** :

```python
# souvenir/main.py
from common.config_loader import load_profiles, resolve_profile

_FALLBACK_EXTRACTION_MODEL = "glm-4.7-flash"
try:
    _profiles = load_profiles()
    _extraction_profile = resolve_profile(_profiles, "memory_extractor")
    extraction_model = _extraction_profile.model
except Exception as exc:
    logger.warning("Could not load memory_extractor profile: %s", exc)
    extraction_model = _FALLBACK_EXTRACTION_MODEL

class Souvenir:
    def __init__(self, litellm_url: str, ...):
        self._extractor = MemoryExtractor(
            litellm_url=litellm_url,
            model=extraction_model  # Dynamic, not hardcoded
        )
```

Ce chargement dynamique permet de changer le modèle d'extraction via configuration sans redéploiement.

### Résilience LLM — pattern XACK

Si le backend LLM (LiteLLM proxy) est indisponible, `SDKExecutor.execute()` lève `SDKExecutionError` (wrapping `APIStatusError` et `APIConnectionError`). L'appelant route vers la DLQ et ACK. Les erreurs transientes non catchées restent en PEL pour re-livraison.

```yaml
# config/profiles.yaml — section résilience dans chaque profil
default:
  model: claude-opus-4-6
  max_turns: 20
  temperature: 0.7
  max_tokens: 2048
  resilience:
    retry_attempts: 3
    retry_delays: [2, 5, 15]   # délais en secondes, backoff exponentiel
    fallback_model: null
```

**Pattern XACK dans `atelier/main.py`** :

```python
# SDKExecutionError → DLQ + ACK (non-retriable)
# Exception générique → laisse en PEL (transient, re-delivery automatique)
except SDKExecutionError as exc:
    await redis_conn.xadd("relais:tasks:failed", {"payload": payload, "reason": str(exc), ...})
    return True  # ACK — dans DLQ, pas perdu
except Exception as exc:
    return False  # pas d'ACK — reste en PEL
```

### Streaming progressif — édition temps réel Discord/Telegram

Pour les canaux supportant l'édition de messages (Discord, Telegram), L'Atelier publie les chunks au fur et à mesure dans `relais:messages:streaming:{channel}:{correlation_id}` :

```python
# atelier/stream_publisher.py
class StreamPublisher:
    """Publishes response chunks for real-time message editing."""
    STREAM_TTL_SECONDS = 300
    STREAM_MAXLEN = 500

    async def push_chunk(self, chunk: str, is_final: bool = False):
        """Append chunk to streaming Redis stream."""
        await self.redis.xadd(
            f"relais:messages:streaming:{self.channel}:{self.correlation_id}",
            {"seq": self.seq, "chunk": chunk, "is_final": int(is_final)},
            maxlen=self.STREAM_MAXLEN,
            approximate=True
        )
        # Set TTL on the stream
        await self.redis.expire(
            f"relais:messages:streaming:{self.channel}:{self.correlation_id}",
            self.STREAM_TTL_SECONDS
        )
        self.seq += 1

    async def finalize(self):
        """Signal end of response stream."""
        await self.push_chunk("", is_final=True)
```

**Aiguilleur (Discord) consomme via:**

```python
# aiguilleur/discord/main.py
STREAM_EDIT_THROTTLE_CHARS = 80
STREAM_READ_BLOCK_MS = 150

async def _handle_streaming_message(self, envelope: Envelope):
    """Send placeholder, read chunks, edit message progressively."""
    msg = await self.channel.send("▌")  # Placeholder
    accumulated = ""

    while True:
        # XREAD relais:messages:streaming:discord:{correlation_id}
        chunks = await redis.xread(
            {f"relais:messages:streaming:discord:{envelope.correlation_id}": "$"},
            block=self.STREAM_READ_BLOCK_MS
        )
        for chunk_data in chunks:
            if chunk_data.get("is_final"):
                # Final edit without cursor
                await msg.edit(content=accumulated)
                break
            chunk = chunk_data.get("chunk", "")
            accumulated += chunk
            if len(accumulated) >= self.STREAM_EDIT_THROTTLE_CHARS:
                await msg.edit(content=accumulated + " ▌")
                await asyncio.sleep(0.2)  # Rate limit edits
```

### Context window compaction

Le Souvenir surveille la taille de l'historique. Quand l'historique dépasse 80% du context window du modèle, un LLM léger (Haiku) génère un résumé qui remplace les anciens messages.

```python
# souvenir/context_store.py
class ContextStore:

    CONTEXT_WINDOW_LIMITS = {
        "claude-opus-4-6":   200_000,
        "claude-sonnet-4-6": 200_000,
        "claude-haiku-4-5":  200_000,
        "qwen3-coder-30b":   32_000,
        "llama3.2":          128_000,
    }
    COMPACTION_THRESHOLD = 0.80  # 80% du context window

    async def get_recent(
        self, session_id: str, limit: int = 20
    ) -> list[dict]:
        """Get recent messages from Redis (cache) with SQLite fallback."""
        # Try Redis first (fast path)
        messages = await self.redis_list.get_recent(session_id, limit)
        if messages:
            return messages

        # Fallback to SQLite if Redis miss
        return await self.long_term.get_recent_messages(session_id, limit)

    async def append_turn(self, session_id: str, user_msg: str, assistant_msg: str):
        """Append user+assistant pair to session history."""
        await self.redis_list.rpush(session_id, {
            "role": "user",
            "content": user_msg,
            "timestamp": time.time()
        })
        await self.redis_list.rpush(session_id, {
            "role": "assistant",
            "content": assistant_msg,
            "timestamp": time.time()
        })
        await self.redis_list.ltrim(session_id, 0, 19)  # Keep last 20
        await self.redis_list.expire(session_id, 86400)  # 24h TTL
```

### Graceful shutdown

```python
# atelier/main.py
shutdown = GracefulShutdown(timeout=30.0)
shutdown.setup()

while not shutdown.is_set():
    for msg_id, payload in await consumer.reclaim_pending():
        t = asyncio.create_task(handle_task(msg_id, payload))
        shutdown.track(t)
    for msg_id, payload in await consumer.read(count=5, block_ms=1000):
        t = asyncio.create_task(handle_task(msg_id, payload))
        shutdown.track(t)

await shutdown.wait_for_tasks()
```

---

## 12. Le Souvenir — mémoire, dual-stream, extraction de faits

**Updated 2026-03-28:** Souvenir now consumes two streams (memory requests from Atelier + observes outgoing messages). Automated memory extraction via fast LLM identifies user facts for long-term storage.

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

Souvenir consomme deux streams :

**Stream 1 : `relais:memory:request`** (Atelier → Souvenir)
- Action `get` : retourner l'historique pour une session donnée
- Flux : Atelier envoie `{action: "get", session_id, correlation_id}` → Souvenir répond via `relais:memory:response`

**Stream 2 : `relais:messages:outgoing:{channel}`** (observe) — NOUVEAU
- Observer toutes les réponses assistantes (from all channels)
- Pour chaque message sortant : extraire les faits utilisateur, archiver en SQLite, mettre en cache Redis

```python
# souvenir/main.py — dual consumer groups
consumer_get = StreamConsumer(redis, "relais:memory:request", "souvenir_memory")
consumer_out = StreamConsumer(redis, f"relais:messages:outgoing:*", "souvenir_outgoing")

while not shutdown.is_set():
    # Handle memory get requests
    for msg_id, payload in await consumer_get.read(count=5, block_ms=100):
        await _handle_get_request(payload)
        await consumer_get.ack(msg_id)

    # Observe outgoing messages (extract facts, archive)
    for channel_name in ["discord", "telegram", "rest"]:
        stream = f"relais:messages:outgoing:{channel_name}"
        for msg_id, payload in await consumer_out.read(stream, count=1, block_ms=100):
            await _handle_outgoing(payload)
            await consumer_out.ack(msg_id)
```

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
   d. long_term_store.archive(envelope)  ← existing (SQLite messages)
   e. memory_extractor.extract(envelope) ← NEW (identify user facts)
```

### Memory extraction — identification automatique de faits utilisateur (Axe B & C)

**Updated 2026-03-29:** Memory extraction now uses a dedicated `memory_extractor` profile for dynamic model selection.

Nouvelle étape dans `_handle_outgoing` :

```python
# souvenir/main.py — initialization with dynamic profile loading
from common.config_loader import load_profiles, resolve_profile

_FALLBACK_EXTRACTION_MODEL = "glm-4.7-flash"
try:
    _profiles = load_profiles()
    _extraction_profile = resolve_profile(_profiles, "memory_extractor")
    extraction_model = _extraction_profile.model
except Exception as exc:
    logger.warning("Could not load memory_extractor profile, using fallback: %s", exc)
    extraction_model = _FALLBACK_EXTRACTION_MODEL

class Souvenir:
    def __init__(self, ...):
        self._extractor = MemoryExtractor(litellm_url=litellm_url, model=extraction_model)
```

```python
# souvenir/memory_extractor.py — NOUVEAU
class MemoryExtractor:
    """Fast LLM call to extract durable user facts from conversation.

    Model selection from memory_extractor profile (config/profiles.yaml).
    Fallback: glm-4.7-flash (previously hardcoded as gpt-3.5-turbo).
    """

    def __init__(self, litellm_url: str, model: str):
        self.litellm_url = litellm_url
        self.model = model  # Dynamically loaded from profile
        self.http_client = httpx.AsyncClient(base_url=litellm_url)

    async def extract(self, envelope: Envelope) -> list[UserFact]:
        """Analyze user message + assistant reply, extract structured facts."""
        user_msg = envelope.metadata.get("user_message", "")
        assistant_reply = envelope.content

        prompt = f"""Analyse cet échange utilisateur-assistant.
        Extrais les faits durables sur l'utilisateur (préférences, contraintes, objectifs).

        Échange:
        Utilisateur: {user_msg}
        Assistant: {assistant_reply}

        Réponds en JSON strict: [{{"fact": "...", "category": "preference|constraint|goal|context", "confidence": 0.0-1.0}}]
        """

        # Fast LLM call via LiteLLM proxy (profil "memory_extractor" model + settings)
        response = await self.http_client.post(
            "/v1/chat/completions",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 512,
                "top_p": 1.0
            },
            timeout=10
        )

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            facts = json.loads(content)
            # Filter confidence > 0.7
            return [f for f in facts if f.get("confidence", 0) > 0.7]
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Memory extraction parse error: {response.text}")

    async def store(self, sender_id: str, facts: list[UserFact]):
        """Upsert facts into SQLite user_facts table (fire-and-forget)."""
        for fact in facts:
            # Hash (sender_id, fact) for idempotency
            fact_hash = hashlib.sha256(f"{sender_id}:{fact['fact']}".encode()).hexdigest()
            await self.long_term.upsert_fact(
                id=fact_hash,
                sender_id=sender_id,
                fact=fact["fact"],
                category=fact["category"],
                confidence=fact["confidence"],
                source_corr=fact.get("source_corr"),
                created_at=time.time(),
                updated_at=time.time()
            )
```

**Table `user_facts` (Alembic migration):**

```sql
CREATE TABLE user_facts (
    id TEXT PRIMARY KEY,
    sender_id TEXT NOT NULL,
    fact TEXT NOT NULL,
    category TEXT,  -- preference, constraint, goal, context
    confidence REAL,
    source_corr TEXT,  -- correlation_id du message source
    created_at REAL,
    updated_at REAL,
    INDEX idx_sender_id (sender_id),
    INDEX idx_category (category)
);
```

### Scopes mémoire

| Scope | Historique | Faits | Profils |
|---|---|---|---|
| `global` | Toutes sessions récentes | Benjamin + tous | ADMIN |
| `own` | Session courante | Benjamin | USER, SUPERVISOR |
| `sender` | Sessions avec l'expéditeur | Benjamin + expéditeur | AUTO_REPLY |
| `task` | Contexte parent uniquement | Aucun | SUB_AGENT |

### Pagination native

```python
# souvenir/long_term_store.py
class LongTermStore:

    async def query(
        self,
        user_id: str,
        limit: int = 20,
        offset: int = 0,
        since: datetime | None = None,
        until: datetime | None = None,
        search: str | None = None
    ) -> PaginatedResult:
        """
        Paginated query on long-term memory.
        Used by Le Vigile for admin queries:
          "montre page 2 des logs d'hier"
          "recherche les mentions de la MR #42"
        """
        query = "SELECT * FROM facts WHERE user_id = ?"
        params = [user_id]

        if since:
            query += " AND created_at >= ?"
            params.append(since)
        if until:
            query += " AND created_at <= ?"
            params.append(until)
        if search:
            query += " AND content LIKE ?"
            params.append(f"%{search}%")

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = await self.db.fetch_all(query, params)
        total = await self.db.fetch_one(
            "SELECT COUNT(*) FROM facts WHERE user_id = ?", [user_id]
        )

        return PaginatedResult(
            items=[dict(r) for r in rows],
            total=total[0],
            limit=limit,
            offset=offset,
            has_more=(offset + limit) < total[0]
        )
```

---

## 13. Le Veilleur — planification, backup, rétention

### Pure publisher

```python
# veilleur/main.py
class Veilleur:
    """
    Pure publisher — no LLM, no direct agent execution.
    Publishes AgentTasks to relais:tasks Stream.
    L'Atelier executes them with SCHEDULER_AGENT profile.
    """

    async def tick(self):
        overdue = self.find_overdue_checks()
        for check in overdue:
            await self.stream_producer.publish("relais:tasks", {
                "profile_id": "SCHEDULER_AGENT",
                "origin": "veilleur",
                "channel": "system",
                "user_id": "usr_system",
                "session_id": f"cron-{check.name}-{uuid4()}",
                "system_prompt": check.prompt,
                "message": f"Execute: {check.name}",
            })
            self.update_timestamp(check.name)
```

### HEARTBEAT.md — tâches complètes

```markdown
# HEARTBEAT.md

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

## Bridge health check
- Cadence: every 6 hours
- Prompt: GET /health aiguilleur-whatsapp et aiguilleur-signal.
  Si déconnecté : notifier ADMIN (urgency: high) + tenter restart.

## Log retention cleanup
- Cadence: daily at 03:30
- Prompt: SYSTEM:cleanup_logs
  Supprime JSONL > 90j. Purge SQLite archiviste > 1 an (hors audit). Vacuum SQLite.

## Backup
- Cadence: daily at 04:00 (if backup.enabled in config)
- Prompt: SYSTEM:backup
  SQLite snapshot souvenir + archiviste. rsync vers backup_path.

## Auto-forgeron run
- Cadence: daily at 02:00
- Prompt: SYSTEM:start_forgeron
```

### Backup — configurable

```yaml
# config/config.yaml
backup:
  enabled: true                         # activé/désactivé
  path: "/Volumes/Backup/relais"        # chemin configurable
  files:
    - souvenir/relais_memory.db         # SQLite Le Souvenir
    - archiviste/logs/relais.db         # SQLite L'Archiviste
    - atelier/skills/                   # tous les skills
    - soul/                             # personnalité JARVIS
    - config/                           # configuration
  sqlite_backup_api: true               # utilise .backup() SQLite (safe en concurrent)
  rsync_options: "-av --delete"
```

```python
# veilleur/backup_handler.py
class BackupHandler:
    """
    Triggered by SYSTEM:backup command from Le Veilleur.
    Uses SQLite .backup() API for safe concurrent backup.
    """

    async def run(self, config: BackupConfig):
        if not config.enabled:
            return

        backup_path = Path(config.path) / datetime.now().strftime("%Y-%m-%d")
        backup_path.mkdir(parents=True, exist_ok=True)

        for db_file in [f for f in config.files if f.endswith(".db")]:
            await self._backup_sqlite(db_file, backup_path)

        await self._rsync_files(
            [f for f in config.files if not f.endswith(".db")],
            backup_path,
            config.rsync_options
        )

        await publish_event(self.redis, "relais:events:backup_completed", {
            "path": str(backup_path),
            "files_count": len(config.files),
        })

    async def _backup_sqlite(self, db_path: str, dest: Path):
        """Uses SQLite .backup() API — safe while DB is in use."""
        import sqlite3
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(str(dest / Path(db_path).name))
        src.backup(dst)
        dst.close()
        src.close()
```

### Rétention des logs

```yaml
# config/config.yaml
retention:
  jsonl_days: 90          # fichiers JSONL — nettoyage quotidien par Le Veilleur
  sqlite_days: 365        # enregistrements SQLite L'Archiviste
  audit_days: null        # logs d'audit (commandes admin) — jamais supprimés
  media_hours: 24         # fichiers médias temporaires — TTL 24h
```

---

## 14. Le Forgeron — auto-apprentissage & versioning skills

### Batch processor isolé

```python
# forgeron/main.py
class Forgeron:
    """
    Reads Archiviste's SQLite directly (shared local file).
    Publishes skills to relais:skills:new (Stream).
    Exits cleanly when done (autorestart=false in supervisord).
    """

    async def run(self):
        patterns = await self.analyzer.find_repeated_patterns(
            db_path="archiviste/logs/relais.db",
            min_occurrences=config.forgeron.min_occurrences,
            min_days=config.forgeron.min_days,
            score_threshold=config.forgeron.score_threshold
        )

        for pattern in patterns:
            skill_content = await self.generator.generate(pattern)
            await self.stream_producer.publish("relais:skills:new", {
                "name": pattern.suggested_name,
                "content": skill_content,
                "pattern": pattern.to_dict(),
                "auto_approve": config.forgeron.auto_approve,
            })

        sys.exit(0)
```

### Versioning des skills — par nom de fichier

```
Pas de Git, pas de SQLite de versioning.
Les fichiers auto-générés sont nommés avec la date et jamais supprimés.

atelier/skills/auto/
  SKILL_auto_mr_review_20260327.md    ← version courante dans CLAUDE.md
  SKILL_auto_mr_review_20260315.md    ← version précédente — conservée
  SKILL_auto_mr_review_20260301.md    ← version encore plus ancienne

Rollback :
  Le Vigile modifie CLAUDE.md pour pointer vers une version antérieure.
  "Vigile : utilise la version du 15 mars pour le skill mr_review"
  → Le Vigile met à jour le chemin dans CLAUDE.md
  → La prochaine session charge l'ancienne version

CLAUDE.md — registre des skills actifs
  # Skills actifs (modifié par Le Vigile)
  - mr_review → atelier/skills/auto/SKILL_auto_mr_review_20260327.md
  - rag_index → atelier/skills/manual/SKILL_rag_index.md
```

---

## 15. L'Archiviste — pure observer avec pipeline observation (Axe A)

**Updated 2026-03-29:** Archiviste now runs two parallel consumer groups for comprehensive pipeline visibility:
1. `archiviste_logs_group` — observes critical logs from `relais:logs` (existing)
2. `archiviste_pipeline_group` — observes all pipeline streams (NEW)

```python
# archiviste/main.py
class Archiviste:
    """
    Pure observer — consumes relais:logs Stream + pipeline streams + relais:events:* Pub/Sub.
    Writes to JSONL + SQLite.
    Never publishes to Redis.
    Handles retention cleanup on SYSTEM:cleanup_logs command.
    """

    async def run(self):
        # Consumer group 1: Critical logs
        log_consumer = StreamConsumer(
            redis=self.redis, stream="relais:logs",
            group="archiviste_logs_group", consumer="archiviste-logs-1"
        )
        await log_consumer.ensure_group()

        # Consumer group 2: Pipeline observation (NEW)
        pipeline_consumer = StreamConsumer(
            redis=self.redis,
            group="archiviste_pipeline_group", consumer="archiviste-pipeline-1"
        )
        await pipeline_consumer.ensure_group()

        pubsub = self.redis.pubsub()
        await pubsub.psubscribe("relais:events:*")

        await asyncio.gather(
            self._consume_log_stream(log_consumer),
            self._process_pipeline_streams(pipeline_consumer),  # NEW
            self._consume_events(pubsub),
        )

    async def _process_pipeline_streams(self, pipeline_consumer):
        """
        Observes all pipeline streams (Axe A):
        - relais:messages:incoming:* (per channel)
        - relais:security
        - relais:tasks
        - relais:tasks:failed (DLQ)
        - relais:messages:outgoing:* (per channel)

        For each message, logs: [{cid[:8]}] {sender_id} → {stream} | traces={traces} | "{content_preview}..."
        """
        pipeline_streams = [
            "relais:messages:incoming:discord",
            "relais:messages:incoming:telegram",
            "relais:security",
            "relais:tasks",
            "relais:tasks:failed",
            "relais:messages:outgoing:discord",
            "relais:messages:outgoing:telegram",
        ]

        while not self.shutdown.is_set():
            for stream in pipeline_streams:
                try:
                    messages = await pipeline_consumer.read(
                        stream=stream, count=1, block_ms=100
                    )
                    for msg_id, payload in messages:
                        envelope = Envelope.from_json(payload.get("envelope", "{}"))
                        cid = envelope.correlation_id[:8]
                        sender = envelope.sender_id or "system"
                        content_preview = envelope.content[:60].replace("\n", " ")
                        traces = envelope.metadata.get("traces", [])

                        self.logger.info(
                            f"[{cid}] {sender} → {stream} | traces={traces} | \"{content_preview}...\"",
                            extra={"stream": stream, "correlation_id": envelope.correlation_id}
                        )
                        await pipeline_consumer.ack(msg_id)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.warning(f"Pipeline stream read error ({stream}): {e}")

### Enriched Brick Logs (Axe B)

**New in 2026-03-29:** All core bricks (Portail, Sentinelle, Atelier, Souvenir) now enrich their `relais:logs` entries with three fields:

```python
# All bricks now enrich logs with:
enriched_log = {
    "timestamp": time.time(),
    "level": "info|warning|error",
    "message": "...",
    # NEW Axe B fields:
    "correlation_id": envelope.correlation_id,  # UUID tracking across pipeline
    "sender_id": envelope.sender_id,            # e.g. "discord:805123..."
    "content_preview": envelope.content[:60].replace("\n", " "),  # first 60 chars
}

# Each brick publishes to relais:logs:
await redis.xadd("relais:logs", enriched_log)
```

Archiviste re-emits these enriched entries with correlation_id prefix:

```python
# archiviste/main.py — in _consume_log_stream()
cid = payload.get("correlation_id", "unknown")[:8]
sender = payload.get("sender_id", "system")
message = payload.get("message", "")
# Log line: [a1b2c3d4] discord:805123... | message text
self.logger.info(f"[{cid}] {sender} | {message}")
```

This enables:
- End-to-end request tracking via correlation_id
- Quick identification of message origin (sender_id)
- Content preview for debugging without reading full messages

    async def cleanup_retention(self, config: RetentionConfig):
        """Triggered by SYSTEM:cleanup_logs from Le Veilleur."""
        if config.jsonl_days:
            cutoff = datetime.now() - timedelta(days=config.jsonl_days)
            for f in Path("archiviste/logs").glob("*.jsonl"):
                if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    f.unlink()

        if config.sqlite_days:
            cutoff_ts = (datetime.now() - timedelta(days=config.sqlite_days)).isoformat()
            await self.db.execute(
                "DELETE FROM logs WHERE level != 'audit' AND ts < ?",
                [cutoff_ts]
            )
            await self.db.execute("VACUUM")
```

---

## 16. Le Crieur — push proactif & multi-canal

### Stratégie multi-canal sans déduplication

```yaml
# config/config.yaml
crieur:
  routing_strategy:
    normal:   last_active   # 1 canal — évite le bruit quotidien
    high:     all_active    # tous les canaux actifs — intentionnel
    critical: all_active    # tous les canaux + notification OS native
    # Pour high et critical : recevoir sur plusieurs canaux est voulu.
    # L'objectif est de s'assurer que l'utilisateur voit l'alerte.
```

### Résolution des destinataires

```python
# crieur/router.py
async def resolve_targets(self, push: PushEnvelope) -> list[tuple[str, str]]:
    if push.target_user_id:
        return await self._resolve_for_user(push.target_user_id, push.urgency)

    if push.target_role:
        result = []
        for uid in await self._get_users_by_role(push.target_role):
            result.extend(await self._resolve_for_user(uid, push.urgency))
        return result

    if push.session_id:
        session = await self.redis.hgetall(f"relais:sessions:{push.session_id}")
        if session:
            return await self._resolve_for_user(session["user_id"], push.urgency)

    return []

async def _resolve_for_user(self, user_id: str, urgency: str) -> list[tuple[str, str]]:
    strategy = config.crieur.routing_strategy[urgency]
    active = await self.redis.hgetall(f"relais:active_sessions:{user_id}")

    if not active:
        preferred = config.users.get(user_id, {}).get("preferred_channel")
        return [(user_id, preferred)] if preferred else []

    channels = {ch: float(ts) for ch, ts in active.items()}

    if strategy == "last_active":
        best = max(channels.items(), key=lambda x: x[1])
        return [(user_id, best[0])]
    else:  # all_active — intentionnel pour high et critical
        return [(user_id, ch) for ch in channels]
```

---

## 17. Le Guichet — webhooks entrants

```python
# guichet/main.py
@app.post("/webhook/{source}")
async def receive_webhook(source: str, request: Request,
                           x_hub_signature: str = Header(None)):
    if source not in SOURCES:
        raise HTTPException(404)
    body = await request.body()
    secret = get_secret(f"WEBHOOK_SECRET_{source.upper()}")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(f"sha256={expected}", x_hub_signature or ""):
        raise HTTPException(403, "Invalid signature")
    payload = await request.json()
    push = PushEnvelope(source=f"guichet:{source}", ...)
    await redis.publish("relais:push:high", push.to_json())
    return {"status": "received"}
```

---

## 18. Le Vigile — administration NLP & hot reload

### Hot config reload

```python
# vigile/main.py
class Vigile:

    async def handle_admin_command(self, text: str, user: User) -> str:
        intent = await self.nlp_parser.parse(text)

        match intent.action:
            case "reload_config":
                await self._hot_reload_config()
                return "✅ Configuration rechargée sur toutes les briques."
            # ... autres actions

    async def _hot_reload_config(self):
        """
        Publishes reload signal to all bricks.
        Each brick reloads its config section from disk.
        No restart required.
        """
        await self.redis.publish("relais:admin:reload", json.dumps({
            "action": "reload",
            "files": ["config.yaml", "profiles.yaml",
                      "reply_policy.yaml", "soul/SOUL.md"]
        }))
```

```python
# common/config_loader.py
class ConfigWatcher:
    """
    Each brick subscribes to relais:admin:reload
    and reloads its config section in memory.
    """

    async def watch(self, redis, on_reload: callable):
        pubsub = redis.pubsub()
        await pubsub.subscribe("relais:admin:reload")
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                await on_reload()

# Usage in each main.py:
# config_watcher = ConfigWatcher()
# asyncio.create_task(config_watcher.watch(redis, lambda: reload_my_config()))
```

### Pilotage supervisord

```python
# vigile/supervisord_client.py
class SupervisordClient:
    def __init__(self, url="http://localhost:9001/RPC2"):
        self.server = xmlrpc.client.ServerProxy(url)

    def get_all_status(self) -> list[ProcessStatus]: ...
    def start(self, name: str) -> bool: ...
    def stop(self, name: str) -> bool: ...
    def restart(self, name: str) -> bool: ...
    def get_logs(self, name: str, offset=0, length=2000) -> str: ...
    def start_group(self, group: str) -> bool: ...
    def stop_group(self, group: str) -> bool: ...
```

### Exemples de commandes

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

---

## 19. Le Tableau — TUI bidirectionnel

Layout Textual 3 colonnes avec streaming, notifications push async, et monitoring supervisord en temps réel. Les messages push arrivent sans interrompre la saisie via `call_from_thread`.

```
💬 Messages utilisateur   → fond bleu
🤖 Réponses JARVIS        → streaming token par token
📣 Notifications push     → fond ambre
⚙️ Événements système     → fond gris
```

---

## 20. Le Tisserand — extensions intercepteurs

### Règle de décision fondamentale

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

### Interface développeur

```python
class Interceptor(ABC):
    name: str
    priority: int = 100
    required_permissions: list[str] = []

    async def on_request(self, event) -> RequestEvent | None: return event
    async def on_stream_chunk(self, event) -> StreamChunkEvent | None: return event
    async def on_tool_call_start(self, event) -> ToolCallStartEvent | None: return event
    async def on_command(self, event) -> CommandEvent | None: return event
```

Timeout 2s par intercepteur. Exception → skippé + loggé. RELAIS ne crashe jamais à cause d'une extension.

---

## 21. Le Scrutateur — pure observer

```
GET /metrics                → Prometheus
GET /health                 → statut global
GET /stats                  → résumé (sessions actives, coûts)
GET /trace/{correlation_id} → chemin complet d'une requête
```

Métriques : `relais_requests_total`, `relais_tokens_total`, `relais_request_duration_seconds`, `relais_tool_calls_total`, `relais_errors_total`, `relais_active_sessions`, `relais_daily_cost_usd`, `relais_interceptor_blocks_total`.

Dashboards Grafana dans `scrutateur/grafana/dashboards/`.

---

## 22. SOUL.md & prompts — personnalité JARVIS multi-couches

**Updated 2026-03-28:** Prompts are now organized in a structured directory layout (6 layers), assembled by `soul_assembler.py`.

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

**Layer assembly order :**

| Order | Source | File | Always present |
|-------|--------|------|---|
| 1 | Personality | `soul/SOUL.md` | Yes (error if missing) |
| 2 | Role | `prompts/roles/{role}.md` | No (optional) |
| 3 | User | `prompts/users/{sender_id}.md` | No (optional) |
| 4 | Channel | `prompts/channels/{channel}_default.md` | No (warning if missing) |
| 5 | Policy | `prompts/policies/{reply_policy}.md` | No (optional) |
| 6 | Memory | Injected user facts from SQLite | No (optional) |

```python
# atelier/soul_assembler.py
system_prompt = assemble_system_prompt(
    prompts_dir="~/.relais/prompts",
    channel="discord",
    sender_id="discord:123456789",
    user_role="admin",
    reply_policy="in_meeting",
    user_facts=["Préfère la concision", "Occupé jeudi 14h-15h"]
)
```

### Internationalisation — SOUL.md gère tout

SOUL.md instructions à JARVIS d'utiliser la langue de son interlocuteur. Le LLM détecte automatiquement la langue entrante et répond dans la même langue. Les notifications système natives (macOS/Linux) restent en français car elles sont générées par Le Crieur, pas par le LLM.

```markdown
# soul/SOUL.md (extrait)

**En français par défaut.** Tu détectes automatiquement la langue de ton
interlocuteur et tu bascules vers cette langue pour ta réponse.
Si quelqu'un t'écrit en anglais, tu répondras en anglais.
Si quelqu'un t'écrit en arabe, tu répondras en arabe.
Tu ne mélanges jamais les langues dans une même réponse.
```

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

## 23. Profils — config/profiles.yaml complet

```yaml
profiles:

  # ── Human profiles — SOUL applied ────────────────────────────────────────

  ADMIN:
    type: human
    apply_soul: true
    model: claude-opus-4-6
    base_url: http://localhost:4000
    memory: { context: true, long_term: true, scope: global }
    allowed_tools: ["*"]
    allowed_mcp: ["*"]
    sub_agent_limits: { max_depth: 2, max_token_budget: 50000 }
    llm_resilience:
      retries: 3
      backoff_base: 2
      fallback_model: llama3.2
      fallback_base_url: http://localhost:11434
    guardrails:
      max_tokens_per_day: null
      forbidden_bash_patterns: []
      require_confirmation: [stop_all, revoke_user, delete_memory]

  SUPERVISOR:
    type: human
    apply_soul: true
    model: claude-sonnet-4-6
    base_url: http://localhost:4000
    memory: { context: true, long_term: true, scope: own }
    allowed_tools: [Read, "Bash(git *)", "Bash(docker ps*)", "Bash(docker logs*)"]
    allowed_mcp: ["mcp__gitlab__*", mcp__brave__search, mcp__jcodemunch__read_file]
    sub_agent_limits: { max_depth: 1, max_token_budget: 20000 }
    llm_resilience:
      retries: 3
      backoff_base: 2
      fallback_model: llama3.2
      fallback_base_url: http://localhost:11434
    guardrails:
      max_tokens_per_day: 200000
      forbidden_bash_patterns: ["rm *", "sudo *", "curl * | bash"]
      require_confirmation: ["restart:*"]

  USER:
    type: human
    apply_soul: true
    model: claude-haiku-4-5
    base_url: http://localhost:4000
    memory: { context: true, long_term: true, scope: own }
    allowed_tools: [Read]
    allowed_mcp: [mcp__brave__search]
    sub_agent_limits: { max_depth: 0, max_token_budget: 0 }
    llm_resilience:
      retries: 3
      backoff_base: 2
      fallback_model: llama3.2
      fallback_base_url: http://localhost:11434
      fallback_message: "Service momentanément limité, je continue avec un modèle local."
    guardrails:
      max_tokens_per_day: 50000
      forbidden_bash_patterns: ["*"]
      forbidden_topics: [credentials, "internal system"]

  # ── Technical conversational — SOUL applied ───────────────────────────────

  AUTO_REPLY:
    type: auto_reply
    apply_soul: true
    model: claude-sonnet-4-6
    base_url: http://localhost:4000
    memory: { context: true, long_term: true, scope: sender }
    allowed_tools: [Read, mcp__calendar__read_agenda, mcp__brave__search]
    allowed_mcp: ["mcp__calendar__*", mcp__brave__search]
    sub_agent_limits: { max_depth: 0, max_token_budget: 0 }
    llm_resilience:
      retries: 3
      backoff_base: 2
      fallback_model: llama3.2
      fallback_base_url: http://localhost:11434
    guardrails:
      max_tokens_per_response: 500
      forbidden_topics: [credentials, "banking data"]
      mandatory_signature: "— JARVIS, assistant de Benjamin"
      max_sub_agents: 0

  # ── Silent technical — no SOUL ────────────────────────────────────────────

  SUB_AGENT:
    type: technical
    apply_soul: false
    model: qwen3-coder-30b
    base_url: http://localhost:11434
    memory: { context: true, long_term: false, scope: task }
    allowed_tools: [Read, "Bash(git *)", "mcp__jcodemunch__*"]
    allowed_mcp: ["mcp__jcodemunch__*", mcp__gitlab__get_mr]
    sub_agent_limits: { max_depth: 0, max_token_budget: 0 }
    llm_resilience:
      retries: 2
      backoff_base: 1
      fallback_model: null   # pas de fallback pour les sous-agents
    guardrails:
      max_tokens_per_turn: 2000

  SCHEDULER_AGENT:
    type: technical
    apply_soul: false
    model: claude-haiku-4-5
    base_url: http://localhost:4000
    memory: { context: false, long_term: true, scope: global }
    allowed_tools: [Read, "mcp__gitlab__*", "mcp__calendar__*", mcp__brave__search]
    allowed_mcp: ["*"]
    sub_agent_limits: { max_depth: 1, max_token_budget: 10000 }
    llm_resilience:
      retries: 3
      backoff_base: 2
      fallback_model: llama3.2
      fallback_base_url: http://localhost:11434
    guardrails:
      max_tokens_per_run: 5000

  LEARNER_AGENT:
    type: technical
    apply_soul: false
    model: claude-sonnet-4-6
    base_url: http://localhost:4000
    memory: { context: false, long_term: false, scope: task }
    allowed_tools: [Read, Write]
    allowed_mcp: []
    sub_agent_limits: { max_depth: 0, max_token_budget: 0 }
    llm_resilience:
      retries: 3
      backoff_base: 2
      fallback_model: null
    guardrails:
      max_tokens_per_run: 10000
```

---

## 24. Politique de réponse automatique

Voir section 8. Résumé des modes :

| Mode | Comportement |
|---|---|
| `ignore` | Archivé silencieusement |
| `manual` | Notification owner uniquement |
| `auto_immediate` | Réponse JARVIS sans délai |
| `auto_deferred` | Attente N sec, puis JARVIS |

---

## 25. Gestion des médias

### Principe

Les médias (images, audio, documents) sont stockés temporairement dans `media/` avec un TTL de 24h. L'agent ne voit pas directement le fichier — un lien local est injecté dans le prompt.

```python
# aiguilleur/base.py
async def handle_media(self, raw_media: bytes, mime_type: str,
                       sender_id: str) -> MediaRef:
    """
    Stores media temporarily and returns a reference.
    Called by each relay when a media message is received.
    """
    media_id = str(uuid4())
    ext = mime_to_extension(mime_type)
    path = Path(f"media/{media_id}.{ext}")
    path.write_bytes(raw_media)

    # TTL via a Redis key — cleaned up by Le Veilleur
    await self.redis.setex(f"relais:media:{media_id}", 86400, str(path))

    return MediaRef(
        media_id=media_id,
        path=str(path),
        mime_type=mime_type,
        size_bytes=len(raw_media),
        expires_in_hours=24
    )
```

```python
# portail/prompt_loader.py
def inject_media_into_prompt(prompt: str, media_refs: list[MediaRef]) -> str:
    """
    Injects media references into the task prompt.
    The agent can reference the file path — it does not see the binary content.
    """
    if not media_refs:
        return prompt

    media_section = "\n\n## Fichiers joints\n"
    for ref in media_refs:
        media_section += (
            f"- [{ref.mime_type}] {ref.path} "
            f"({ref.size_bytes // 1024} Ko) "
            f"— disponible pendant {ref.expires_in_hours}h\n"
        )
    return prompt + media_section
```

### Nettoyage

Le Veilleur nettoie les fichiers médias expirés dans son tick quotidien :

```markdown
# HEARTBEAT.md
## Media cleanup
- Cadence: daily at 03:00
- Prompt: SYSTEM:cleanup_media
  Supprime les fichiers dans media/ dont la clé Redis relais:media:* a expiré.
```

### Enveloppe — champ media

```python
@dataclass
class Envelope:
    # ... champs existants ...
    media_refs: list[MediaRef] = field(default_factory=list)
```

---

## 26. Système d'extensions

```
INTERCEPTEUR (tisserand/)  In-process · Python · 2s timeout · return None = blocage
OBSERVER (relais:events:*) Out-of-process · Tout langage · Fire & forget
```

Observer tiers — aucun SDK requis, juste un client Redis avec compte ACL. Exemples dans `observers/`.

---

## 27. Sécurité

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
REDIS_PASS_RELAY=...
REDIS_PASS_GATEWAY=...
REDIS_PASS_SENTINEL=...
REDIS_PASS_WORKSHOP=...
REDIS_PASS_MEMORY=...
REDIS_PASS_SCHEDULER=...
REDIS_PASS_HERALD=...
REDIS_PASS_LEARNER=...
REDIS_PASS_ARCHIVIST=...
REDIS_PASS_WARDEN=...
REDIS_PASS_INTAKE=...
REDIS_PASS_INSPECTOR=...
REDIS_PASS_WEAVER=...

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

```python
class GracefulShutdown:
    def setup(self): ...          # handlers SIGTERM + SIGINT
    def is_set(self) -> bool: ...
    def track(self, task): ...
    async def wait_for_tasks(self): ...
```

`stopwaitsecs` supervisord > timeout Python pour chaque brique.

---

## 28. Corrélation end-to-end

```
Généré une seule fois par L'Aiguilleur.
Propagé via Envelope.from_parent() dans TOUTES les enveloppes dérivées.
Jamais régénéré.
Inclus dans tous les events Redis et tous les logs.
Le Scrutateur expose GET /trace/{correlation_id}.
```

---

## 29. Structure complète du projet

```
/opt/relais/                           ← Installation système (code uniquement)
│                                         Ne contient PAS de données utilisateur
├── .env.example
├── .env                               ← JAMAIS committé
├── .gitignore
├── supervisord.conf
├── README.md
│
├── config/                            ← Templates système (*.default)
│   ├── config.yaml.default
│   ├── profiles.yaml.default
│   ├── users.yaml.default
│   ├── reply_policy.yaml.default
│   ├── mcp_servers.yaml.default
│   ├── redis.conf                     ← reste dans système (Unix socket)
│   ├── litellm.yaml
│   └── HEARTBEAT.md.default
│
├── soul/                              ← Templates SOUL par défaut
│   ├── SOUL.md.default
│   └── variants/
│       ├── SOUL_concise.md.default
│       └── SOUL_professional.md.default
│
├── prompts/                           ← Prompts système par défaut
│   ├── whatsapp_default.md
│   ├── telegram_default.md
│   ├── out_of_hours.md
│   ├── vacation.md
│   └── in_meeting.md
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
│   ├── base.py
│   ├── telegram/main.py
│   ├── discord/main.py
│   ├── slack/main.py
│   ├── matrix/main.py
│   ├── teams/main.py
│   ├── rest/main.py                   ← API Key auth + /docs Swagger
│   ├── tui/main.py
│   ├── whatsapp/
│   │   ├── index.js
│   │   └── package.json
│   └── signal/run.sh
│
├── portail/
│   ├── main.py
│   ├── reply_policy.py
│   └── prompt_loader.py
│
├── sentinelle/
│   ├── main.py
│   ├── acl.py
│   └── guardrails.py
│
├── atelier/
│   ├── main.py
│   ├── executor.py
│   ├── soul_assembler.py
│   └── debounce.py
│
├── souvenir/
│   ├── main.py
│   ├── context_store.py
│   ├── long_term_store.py
│   └── migrations/
│
├── veilleur/
│   ├── main.py
│   ├── backup_handler.py
│   └── cleanup_handler.py
│
├── forgeron/
│   ├── main.py
│   ├── pattern_analyzer.py
│   └── skill_generator.py
│
├── archiviste/
│   ├── main.py
│   └── cleanup_retention.py
│
├── crieur/
│   ├── main.py
│   ├── router.py
│   └── formatter.py
│
├── guichet/
│   ├── main.py
│   ├── sources/
│   └── webhook_acl.py
│
├── vigile/
│   ├── main.py
│   ├── supervisord_client.py
│   └── nlp_parser.py
│
├── tableau/
│   ├── main.py
│   ├── app.py
│   ├── screens/
│   └── widgets/
│
├── tisserand/
│   ├── main.py
│   ├── events.py
│   └── extension_base.py
│
├── scrutateur/
│   ├── main.py
│   └── grafana/
│
├── mcp/
│   ├── calendar/server.py
│   └── brave-search/server.js
│
├── extensions/
│   ├── quota-enforcer/
│   ├── content-filter/
│   └── custom-tools/
│
├── observers/
│   ├── example_python.py
│   ├── example_node.js
│   └── example_go.go
│
└── tests/
    ├── unit/
    ├── integration/
    └── e2e/

─────────────────────────────────────────────────────────────────────────

~/.relais/                             ← Répertoire utilisateur (données & config)
│                                         Résolu par get_relais_home()
│                                         Override via RELAIS_HOME=...
│                                         Créé automatiquement au 1er lancement
├── config/
│   ├── config.yaml                    ← surcharge /opt/relais/config/*.default
│   ├── profiles.yaml
│   ├── users.yaml
│   ├── reply_policy.yaml
│   ├── mcp_servers.yaml
│   └── HEARTBEAT.md
│
├── soul/
│   ├── SOUL.md                        ← personnalité JARVIS personnalisée
│   └── variants/
│       ├── SOUL_concise.md
│       └── SOUL_professional.md
│
├── prompts/                           ← prompts personnalisés
│   ├── marie.md
│   └── family.md
│
├── skills/
│   ├── CLAUDE.md                      ← registre skills actifs (Le Vigile met à jour)
│   ├── manual/                        ← skills écrits à la main
│   │   └── SKILL_my_custom.md
│   └── auto/                          ← générés par Le Forgeron
│       └── SKILL_auto_mr_review_20260327.md
│
├── media/                             ← fichiers médias temporaires (TTL 24h)
│
├── logs/                              ← L'Archiviste écrit ici
│   ├── relais.db                      ← SQLite L'Archiviste + Le Souvenir
│   └── YYYY-MM-DD.jsonl
│
└── backup/                            ← backups (si backup.path non configuré)
```

---

## 30. La Charte RELAIS v12 — définitive

```
┌─────────────────────────────────────────────────────────────────┐
│                    LA CHARTE RELAIS v12                         │
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
│  8.  MCP globaux dans supervisord — contextuels via SDK         │
│  9.  SECRETS dans .env — jamais dans config.yaml               │
│  10. GRACEFUL SHUTDOWN — SIGTERM → finit les tâches in-flight  │
│                                                                  │
│  COMMUNICATION                                                   │
│  11. STREAMS pour topics critiques — PUB/SUB pour monitoring   │
│  12. relais:logs → Stream (audit ne se perd jamais)            │
│  13. PUSH high/critical → tous canaux actifs (intentionnel)    │
│  14. WEBHOOKS via Le Guichet — HMAC avant publication          │
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

## Annexe A — Stack technique

| Lib | Version | Rôle |
|---|---|---|
| supervisord | ≥ 4.x | Gestion processus |
| Redis | ≥ 7.0 | Pub/Sub + Streams |
| LiteLLM | ≥ 1.50 | Proxy LLM multi-modèles |
| claude-agent-sdk | latest | Exécution agents |
| FastAPI | ≥ 0.115 | REST relay + Guichet |
| Textual | ≥ 1.0 | Le Tableau TUI |
| SQLModel | ≥ 0.14 | Le Souvenir |
| Alembic | ≥ 1.13 | Migrations DB |
| APScheduler | ≥ 4.x | Le Veilleur |
| Pydantic v2 | ≥ 2.9 | Validation config |
| structlog | ≥ 24.x | Logs structurés |
| prometheus-client | ≥ 0.20 | /metrics |
| aiohttp | ≥ 3.9 | Loki, ES, webhooks |
| pytz | latest | Fuseaux horaires |
| python-dotenv | ≥ 1.0 | Chargement .env |
| python-telegram-bot | ≥ 21 | Relay Telegram |
| discord.py | ≥ 2.4 | Relay Discord |
| slack-bolt | ≥ 1.20 | Relay Slack |
| matrix-nio | ≥ 0.24 | Relay Matrix |
| botbuilder-python | ≥ 4.x | Relay Teams |
| Baileys (Node.js) | ≥ 6.7 | Relay WhatsApp |
| signal-cli (Java) | ≥ 0.13 | Relay Signal |

---

## Annexe B — Estimation de complexité

| Couche | Effort | Complexité |
|---|---|---|
| Infrastructure (supervisord, Redis, MCP) | 3-4 j | Faible |
| common/ (envelope, streams, shutdown, markdown) | 4-5 j | Faible |
| L'Aiguilleur — canaux natifs Python | 1-2 j / canal | Faible |
| Bridges WhatsApp / Signal | 3-5 j | Moyenne |
| Le Portail + politique de réponse | 4-5 j | Moyenne |
| La Sentinelle + ACL + guardrails | 3-4 j | Faible |
| L'Atelier (résilience LLM + limites sous-agents) | 6-8 j | Moyenne |
| Le Souvenir (compaction + pagination) | 5-6 j | Moyenne |
| Le Veilleur (backup + rétention) | 3-4 j | Faible |
| Le Forgeron (batch) | 4-6 j | Moyenne |
| L'Archiviste (observer + rétention) | 2-3 j | Faible |
| Le Crieur + routing multi-canal | 3-4 j | Moyenne |
| Le Guichet (webhooks) | 2-3 j | Faible |
| Le Vigile + NLP + hot reload | 5-6 j | Moyenne |
| Le Tableau (TUI Textual) | 6-8 j | Moyenne |
| Le Tisserand + extensions natives | 4-5 j | Moyenne |
| Le Scrutateur + Grafana | 3-4 j | Faible |
| SOUL.md + profils + médias | 3-4 j | Faible |
| Tests (unit + integration + e2e) | ongoing | Variable |
| **MVP fonctionnel** | **~10-12 semaines** | |

---

*RELAIS — Document d'Architecture v12 — 2026-03-27*
*Updated 2026-03-28: Phase 2.2-bis — claude-agent-sdk migration, streaming, and memory extraction*
*"Transmettre avec fiabilité, de toute origine vers toute destination."*
*— JARVIS, assistant de Benjamin*
