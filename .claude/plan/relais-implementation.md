# Plan d'implémentation RELAIS
## Basé sur RELAIS_ARCHITECTURE_COMPLETE_v12.md
## Audit consolidé — 2026-03-31

---

## État actuel — MVP Core Loop ✅ opérationnel

Le cycle de base est fonctionnel : Discord → Portail → Sentinelle → Atelier → Souvenir → DeepAgents → Discord, avec L'Archiviste en observer.

### Fichiers implémentés

| Fichier | État | Notes |
|---------|------|-------|
| `common/config_loader.py` | ✅ | Cascade ~/.relais/ > /opt/relais/ > ./ |
| `common/envelope.py` | ✅ | Envelope + PushEnvelope + MediaRef |
| `common/redis_client.py` | ✅ | AsyncRedis factory avec ACL |
| `common/init.py` | ✅ | initialize_user_dir() |
| `portail/main.py` | ✅ | Consumer group, session TTL, logging |
| `sentinelle/main.py` | ✅ | Stub ACL (autorise tout) |
| `atelier/main.py` | ✅ | AgentExecutor, XACK conditionnel, streaming |
| `atelier/agent_executor.py` | ✅ | Wrapper DeepAgents, streaming buffer 80 chars |
| `atelier/mcp_adapter.py` | ✅ | make_mcp_tools() — wrappers LangChain sur McpSessionManager |
| `atelier/tool_policy.py` | ✅ | ToolPolicy — resolve_skills(), filter_mcp_tools() par rôle |
| `atelier/mcp_session_manager.py` | ✅ | Cycle de vie MCP isolé |
| `souvenir/main.py` | ✅ | append/get, Redis List, TTL 24h |
| `archiviste/main.py` | ✅ | JSONL, consumer group multi-streams |
| `aiguilleur/discord/main.py` | ✅ | Bot mentions/DMs, outgoing background task |
| `config/redis.conf` | ✅ | Unix socket .relais/, ACL par brique |
| `supervisord.conf` | ✅ | Dev config (.relais/ paths) |
| `pyproject.toml` | ✅ | Dépendances de base |

---

## Décisions architecturales — Migration Atelier DeepAgents (2026-03-30)

### Zones grises — Décisions prises ✅

| # | Question | Décision |
|---|----------|----------|
| 1 | LiteLLM : garder ou supprimer ? | **Supprimer dès la migration** — DeepAgents appelle les providers directement |
| 2 | Async : si DeepAgents sync-only ? | **Non-bloquant** — DeepAgents est async natif (`ainvoke`/`astream`) via LangGraph |
| 3 | MCP : `langchain-mcp-adapters` ou wrapper manuel ? | **Wrapper manuel `_McpTool(BaseTool)`** — `McpSessionManager` reste inchangé, wrappers LangChain générés par `mcp_adapter.py` |
| 4 | Cycle de vie MCP sessions | **Session globale + reconnexion automatique** — `McpSessionManager` reste, wrappers reconnectent sur stale session |
| 5 | Streaming : granularité vers Redis | **Buffer 80 chars** (`STREAM_BUFFER_CHARS = 80`) — `StreamPublisher` inchangé, buffer dans `AgentExecutor._stream()` |
| 6 | Migration tests | **Réécriture in-place** — `test_sdk_executor.py` → `test_agent_executor.py`, mocks anthropic remplacés par mocks DeepAgents |

### Contrat d'interface `AgentExecutor`

```python
@dataclass(frozen=True)
class AgentResult:
    reply_text: str          # final text reply (may be empty if only tool calls)
    messages_raw: list[dict] # full LangChain graph state (via serialize_messages())

class AgentExecutor:
    def __init__(
        self,
        profile: ProfileConfig,
        soul_prompt: str,
        tools: list[BaseTool],
    ) -> None: ...

    async def execute(
        self,
        envelope: Envelope,
        context: list[dict[str, str]],
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> AgentResult: ...  # was str before commit 4c14b2b
```

- `_TRANSIENT_ERROR_NAMES` : `frozenset{"RateLimitError", "InternalServerError", "APIConnectionError", "APITimeoutError", "ServiceUnavailableError"}` — détectés par nom de classe (provider-agnostic)
- Streaming : `agent.astream(input_data, stream_mode="messages")`, buffer `STREAM_BUFFER_CHARS = 80`
- Erreurs transitoires → propagées unwrapped → `main.py` retourne `False` → message reste en PEL
- Autres exceptions → `AgentExecutionError` → DLQ `relais:tasks:failed` → ACK

### Contrat `make_mcp_tools`

```python
async def make_mcp_tools(session_manager: Any) -> list[BaseTool]
```

- Itère `session_manager.sessions` (dict `server_name → MCP ClientSession`)
- Convention nommage : `{server_name}__{tool_name}`
- Si `list_tools()` lève pour un serveur → warning + skip, autres serveurs traités
- `_McpTool(BaseTool)` : `_arun(**kwargs)` délègue à `session_manager.call_tool(prefixed_name, kwargs)` ; exceptions retournées comme string (loop vivante)

### Contrat `ToolPolicy`

```python
class ToolPolicy:
    def __init__(self, base_dir: Path) -> None
    def resolve_skills(self, metadata_value: object) -> list[str]
    def parse_mcp_patterns(self, metadata_value: object) -> tuple[str, ...]
    def filter_mcp_tools(self, tools: list, metadata_value: object) -> list
```

- `resolve_skills` : retourne des chemins absolus vers les répertoires de skills autorisés pour le rôle (depuis `envelope.metadata["skills_dirs"]`)
- `parse_mcp_patterns` : parse les patterns d'outils MCP autorisés (depuis `envelope.metadata["allowed_mcp_tools"]`)
- `filter_mcp_tools` : filtre la liste de `BaseTool` MCP selon les patterns du rôle
- Les dirs résolus sont passés comme `skills=` à `create_deep_agent()` — pas de `list_skills`/`read_skill` LangChain

### Format modèle

`"provider:model-id"` — ex: `"anthropic:claude-sonnet-4-6"`, `"openai:mon-model"` (LM Studio/Ollama)

`ProfileConfig` expose aussi `base_url: str | None` et `api_key_env: str | None` (obligatoires dans `profiles.yaml`). `base_url` supporte `${VAR}` — fail-fast si variable absente. `api_key_env` est le nom de la variable d'env contenant la clé API (`null` = pas de clé).

### MemoryExtractor migré

Constructeur : `MemoryExtractor(model="provider:model-id")` — ex: `MemoryExtractor(model="anthropic:claude-haiku-4-5")`

Appel LLM : `init_chat_model(model, temperature=0.1, max_tokens=512)` + `ainvoke([SystemMessage, HumanMessage])`

Plus de httpx ni de LiteLLM proxy. Fire-and-forget : toute exception loguée, jamais propagée.

---

## Phase 1 — Consolidation common/ (priorité haute) ✅ DONE

Ces modules sont maintenant présents et fonctionnels.

### 1.1 ✅ `common/shutdown.py` — GracefulShutdown
Implémenté. Pattern SIGTERM/SIGINT pour graceful shutdown.
**Utilisé par:** L'Atelier, Le Veilleur, Le Crieur

### 1.2 ✅ `common/stream_client.py` — Abstraction Redis Streams
Implémenté. StreamConsumer et StreamProducer factorisent le boilerplate XREADGROUP/XACK.
**Utilisé par:** Toutes les briques consommatrices

### 1.3 ✅ `common/event_publisher.py` — Events monitoring
Implémenté. EventPublisher via Pub/Sub Redis pour relais:events:*.
**Utilisé par:** Le Scrutateur, monitoring

### 1.4 ✅ `common/health.py` — Health check standard
Implémenté. health() standard pour tous les bricks.
**Utilisé par:** Le Tableau, Le Vigile, Le Scrutateur

### 1.5 ✅ `common/markdown_converter.py` — Conversion Markdown
Implémenté. Convertisseurs Markdown → Telegram/Slack/plaintext.
**Utilisé par:** Aiguilleur Telegram, Slack, WhatsApp

---

## Phase 2 — Complétion briques MVP (priorité haute) ✅ DONE

### 2.1 ✅ `aiguilleur/base.py` — Classe abstraite
Implémenté. AiguilleurBase ABC avec receive(), send(), format_for_channel().

### 2.2 ✅ Contrat XACK/résilience Atelier — AgentExecutor (CRITIQUE)

**Règle fondamentale : ne jamais XACK avant le succès ou l'épuisement des retries.**

Le contrat est implémenté dans `atelier/agent_executor.py` via `AgentExecutor.execute()` :

- **Erreurs transitoires** (`_TRANSIENT_ERROR_NAMES` : `RateLimitError`, `InternalServerError`, `APIConnectionError`, `APITimeoutError`, `ServiceUnavailableError`) → propagées unwrapped depuis `AgentExecutor.execute()` → `atelier/main.py` reçoit l'exception → `return False` → message **reste en PEL** pour re-livraison automatique
- **Toute autre exception** → wrappée en `AgentExecutionError` → routée vers DLQ `relais:tasks:failed` → ACK (message dans DLQ, pas perdu)
- **Succès** → publié sur `relais:messages:outgoing:{channel}` → ACK

**Dead Letter Queue `relais:tasks:failed` :**
- Stream Redis (même garantie at-least-once)
- L'Archiviste l'observe et alerte (niveau ERROR)
- Format : `{payload: envelope_json, reason: str, attempts: int, failed_at: timestamp}`
- Le Veilleur peut le rejouer manuellement (future phase)

**Autres fichiers atelier/ :**
- `atelier/soul_assembler.py` — assembly SOUL + long-term + history + task
- `atelier/debounce.py` — anti-flood / debounce logique

### 2.3 ✅ Refactoring `portail/` — split selon plan DONE
- ✅ `portail/reply_policy.py` — chargement reply_policy.yaml, logique de filtrage
- ✅ `portail/prompt_loader.py` — chargement prompts personnalisés depuis ~/.relais/prompts/

### 2.4 ✅ Refactoring `sentinelle/` — ACL réelle DONE
- ✅ `sentinelle/acl.py` — chargement users.yaml, vérification ACL par user_id + canal
- ✅ `sentinelle/guardrails.py` — filtres de contenu (hooks pre/post LLM)

### 2.5 ✅ Refactoring `souvenir/` — split selon plan DONE
- ~~`souvenir/context_store.py`~~ **supprimé** (remplacé par stockage direct dans LongTermStore + blob messages_raw)
- ✅ `souvenir/long_term_store.py` — SQLite via SQLModel (mémoire longue durée, upsert par correlation_id)
- ✅ `souvenir/migrations/` — Alembic migrations
- **supprimé** `souvenir/handlers/get_handler.py` (action `get` retirée du bus mémoire)

### 2.6 ✅ `archiviste/cleanup_retention.py` DONE
- Rétention configurable : JSONL 90j, SQLite 1 an, audit ∞

---

## Phase 3 — Templates système (priorité haute) ✅ DONE

Ces fichiers default sont copiés dans ~/.relais/ au premier lancement par `initialize_user_dir()`.

### 3.1 ✅ Fichiers default créés dans `config/` DONE
- ✅ `config/config.yaml.default` — configuration système par défaut
- ✅ `config/atelier/profiles.yaml.default` — profils LLM (model, tools, memory, resilience)
- ✅ `config/portail.yaml.default` — registry utilisateurs (admin, user, usr_system)
- ✅ `config/sentinelle.yaml.default` — ACL Sentinelle
- ✅ `config/atelier/mcp_servers.yaml.default` — MCP servers globaux/contextuels
- ✅ `config/atelier.yaml.default` — config comportementale Atelier (progress events)
- ✅ `config/HEARTBEAT.md.default` — tâches CRON par défaut

### 3.2 ✅ Fichiers SOUL créés dans `soul/` DONE
- ✅ `soul/SOUL.md.default` — personnalité JARVIS (référence plan section 22)
- ✅ `soul/variants/SOUL_concise.md.default`
- ✅ `soul/variants/SOUL_professional.md.default`

### 3.3 ✅ Prompts système créés dans `prompts/` DONE
- ✅ `prompts/whatsapp_default.md`
- ✅ `prompts/telegram_default.md`
- ✅ `prompts/out_of_hours.md`
- ✅ `prompts/vacation.md`
- ✅ `prompts/in_meeting.md`

---

## Phase 5a — Corrections MCP & Profils (2026-03-30) ✅ DONE

### 5a.1 ✅ Format YAML `mcp_servers.yaml` — clé racine `mcp_servers:` DONE

**Problème :** les fichiers `mcp_servers.yaml.default` et `.relais/config/mcp_servers.yaml` définissaient `global:` et `contextual:` à la racine, sans clé `mcp_servers:`. Seul `load_mcp_servers()` était cohérent ; la documentation et les commentaires YAML étaient incohérents.

**Correction :** format canonique normalisé — clé racine `mcp_servers:` obligatoire, `global:` et `contextual:` imbriqués dessous. Les fixtures de tests mises à jour en conséquence.

**Fichiers modifiés :** `config/atelier/mcp_servers.yaml.default`, `tests/test_mcp_loader.py`

### 5a.2 ✅ Support transport SSE dans `McpServerConfig` et `McpSessionManager` DONE

**Problème :** `McpServerConfig` n'avait ni champ `type` ni champ `url`. La documentation YAML mentionnait les serveurs SSE mais le code ignorait ce transport.

**Correction :**
- `McpServerConfig` : ajout de `type: str = "stdio"` et `url: str | None = None`
- `load_for_sdk()` : retourne `{type, url, env?}` pour SSE vs `{type, command, args, env?}` pour stdio
- `McpSessionManager` : branche sur `cfg.get("type", "stdio")` — stdio → `stdio_client`, sse → `sse_client` (guard `_SSE_AVAILABLE`), inconnu → warning + skip
- Fixtures SSE ajoutées dans `tests/test_mcp_loader.py`

**Fichiers modifiés :** `atelier/mcp_loader.py`, `atelier/mcp_session_manager.py`, `tests/test_mcp_loader.py`

### 5a.3 ✅ Suppression section `subagents:` DONE

**Problème :** `profile_loader.py` contenait encore `max_agent_depth` (vestige de l'architecture sous-agents abandonnée).

**Correction :** suppression de `max_agent_depth` dans `ProfileConfig` et `config/atelier/profiles.yaml.default`. Mise à jour du document fondateur et de CLAUDE.md.

**Fichiers modifiés :** `atelier/profile_loader.py`, `config/atelier/profiles.yaml.default`, `plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md`, `CLAUDE.md`

### 5a.4 ✅ Extraction McpSessionManager — refactoring lisibilité DONE (2026-03-30)

**Problème :** `SDKExecutor` (ancienne brique) mêlait logique LLM et infrastructure MCP. Avec la migration DeepAgents, `McpSessionManager` est resté isolé et sert de pont entre la session MCP et `mcp_adapter.py::make_mcp_tools()`.

**Architecture actuelle :**
- `McpSessionManager` (`atelier/mcp_session_manager.py`) : cycle de vie MCP (démarrage stdio/SSE, sessions internes, dispatch avec `asyncio.wait_for`, timeout configurable)
- `mcp_adapter.py::make_mcp_tools(session_manager)` : génère des wrappers `_McpTool(BaseTool)` depuis les sessions actives
- `AgentExecutor` reçoit une `list[BaseTool]` directement — ignorant la nature MCP ou interne des outils

**Tests MCP :** `tests/test_mcp_session_manager.py` (6 tests), `tests/test_mcp_adapter.py`

### 5a.5 ✅ `mcp_timeout` et `mcp_max_tools` — supprimés avec migration DeepAgents

Ces champs existaient dans `ProfileConfig` pour le SDKExecutor (limite d'outils MCP passés au modèle Anthropic, timeout par appel). Avec la migration DeepAgents :
- `mcp_timeout` : géré par `McpSessionManager` via `asyncio.wait_for` directement
- `mcp_max_tools` : plus pertinent — DeepAgents gère la liste d'outils en interne
- Les deux champs sont supprimés de `ProfileConfig` et `config/profiles.yaml.default`

### 5a.6 ✅ Multi-provider LLM — `base_url` et `api_key_env` dans `ProfileConfig` DONE (2026-04-01)

`ProfileConfig` a été étendu avec deux champs obligatoires :
- `base_url: str | None` — endpoint custom (ex: LM Studio, déploiement privé). Supporte `${VAR}` (fail-fast si non définie).
- `api_key_env: str | None` — nom de la variable d'env contenant la clé API.

`_resolve_profile_model()` dans `agent_executor.py` construit un `BaseChatModel` via `init_chat_model()` quand l'un des deux est non-null, sinon passe le string `model` directement à `create_deep_agent`.

Providers supportés : Anthropic, OpenRouter, Ollama, LM Studio (OpenAI-compatible).
Nouvelles dépendances : `langchain-openrouter`, `langchain-ollama`, `langchain-mistralai`, `langchain-deepseek`.

### 5a.7 ✅ Discord typing indicator DONE (2026-04-01)

`_RelaisDiscordClient` affiche l'indicateur "est en train d'écrire" dès réception d'un message, jusqu'à l'envoi de la réponse ou 120 s (timeout de sécurité). Implémenté via `_typing_loop` (tâche asyncio) + `_cancel_typing`.

### 5a.8 ✅ Scoping config Atelier + ProgressConfig DONE (2026-04-03)

**Problème :** `common/profile_loader.py` était partagé entre Atelier et Souvenir mais Souvenir n'en avait plus besoin. Les fichiers `profiles.yaml` et `mcp_servers.yaml` étaient à la racine de `config/`, sans isolation par brique.

**Correction :**
- `common/profile_loader.py` → `atelier/profile_loader.py` ; chemin config : `atelier/profiles.yaml`
- `config/profiles.yaml.default` → `config/atelier/profiles.yaml.default`
- `config/mcp_servers.yaml.default` → `config/atelier/mcp_servers.yaml.default` ; `_FILENAME` dans `mcp_loader.py` mis à jour
- `common/init.py` : `DEFAULT_FILES` mis à jour (crée `config/atelier/`)
- Nouveau `atelier/progress_config.py` : `ProgressConfig` dataclass (frozen) + `load_progress_config()` depuis `atelier.yaml`
- Nouveau `config/atelier.yaml.default` : master switch `progress.enabled`, per-event flags, `publish_to_outgoing`, `detail_max_length`
- `StreamPublisher` : accepte `progress_config: ProgressConfig | None` ; filtre par event, tronque detail, honore `publish_to_outgoing`
- `atelier/main.py` : charge `_progress_config` au démarrage, le passe à `StreamPublisher`
- Discord adapter : affiche tous les événements progress (plus seulement `tool_call`) au format `{event} : [{detail}]`

**Fichiers modifiés :** `atelier/profile_loader.py` (déplacé depuis `common/`), `atelier/main.py`, `atelier/agent_executor.py`, `atelier/mcp_session_manager.py`, `atelier/mcp_loader.py`, `atelier/souvenir_backend.py`, `atelier/stream_publisher.py`, `common/init.py`, `aiguilleur/channels/discord/adapter.py`

**Fichiers créés :** `atelier/progress_config.py`, `config/atelier.yaml.default`, `config/atelier/profiles.yaml.default`, `config/atelier/mcp_servers.yaml.default`

### 5a.9 ✅ Capture historique complet par tour — AgentResult + messages_raw DONE (2026-04-03)

**Commit :** `4c14b2b refactor(souvenir+atelier): capture full message history per turn`

**Problème :** Souvenir ne stockait qu'une paire `user/assistant` (deux messages) par tour. L'historique interne de l'agent (tool calls, observations, messages intermédiaires) était perdu après chaque tour, rendant le contexte reconstitué partiel.

**Correction :**
- `AgentExecutor.execute()` retourne désormais `AgentResult(reply_text, messages_raw)` au lieu de `str`
- `atelier/message_serializer.py` : `serialize_messages()` aplatit l'état LangGraph en une liste JSON-sérialisable
- `atelier/main.py` : stampe `envelope.metadata["messages_raw"] = result.messages_raw` sur l'enveloppe sortante
- `souvenir/main.py` : lit `messages_raw` depuis l'enveloppe et stocke un blob JSON complet par tour (pas deux messages séparés)
- `souvenir/long_term_store.py` : upsert sur `correlation_id` — champs `user_content` + `assistant_content` + `messages_raw JSON`
- Clé Redis : `relais:context:{user_id}` → `relais:context:{session_id}` (scope session)
- `souvenir/context_store.py` **supprimé** — logique absorbée par `LongTermStore` + `_handle_outgoing_message()`
- `souvenir/handlers/get_handler.py` **supprimé** — action `get` retirée du bus mémoire

**Fichiers modifiés :** `atelier/agent_executor.py`, `atelier/message_serializer.py`, `atelier/main.py`, `souvenir/main.py`, `souvenir/long_term_store.py`, `souvenir/handlers/base.py`, `souvenir/handlers/clear_handler.py`, `souvenir/handlers/__init__.py`, `launcher.py`, `pyproject.toml`

**Fichiers supprimés :** `souvenir/context_store.py`, `souvenir/handlers/get_handler.py`, `souvenir/migrations/versions/002_add_user_facts_and_archived_messages.py`

**Fichiers créés :** `souvenir/migrations/versions/002_add_archived_messages.py`, `atelier/message_serializer.py`

---

## Phase 4 — Nouvelles briques (priorité moyenne)

### 4.1 `crieur/` — Push proactif multi-canal
**Taxonomie:** Transformer
**Consomme:** `relais:push:{urgency}` (Pub/Sub)
**Publie:** `relais:notifications:{role}` → aiguilleurs cibles
**Fichiers:** main.py, router.py, formatter.py
**Priorité supervisord:** 10

### 4.2 `veilleur/` — Planification CRON + backup
**Taxonomie:** Pure Publisher
**Lit:** HEARTBEAT.md, config backup
**Publie:** `relais:tasks` (tâches planifiées)
**Fichiers:** main.py, backup_handler.py, cleanup_handler.py
**Priorité supervisord:** 10
**Dépendance:** APScheduler ≥ 4.x

### 4.3 `aiguilleur/rest/` — Canal REST/SSE + Webhooks HMAC
**Taxonomie:** Relay (canal Aiguilleur)
**Reçoit:** HTTP POST `/message` + `POST /webhook/{source}` (HMAC validé)
**Expose:** SSE `GET /stream/{correlation_id}` pour streaming token-par-token
**Publie:** `relais:messages:incoming:rest`
**Fichiers:** adapter.py, webhook_acl.py
**Dépendance:** FastAPI, aiohttp

### 4.4 `forgeron/` — Génération skills auto (batch)
**Taxonomie:** Batch Processor (lancé 1×/jour, exit)
**Lit:** SQLite (archiviste), patterns récurrents
**Écrit:** `~/.relais/skills/auto/` SKILL_auto_*.md
**Publie:** `relais:skills:new` → Vigile
**Fichiers:** main.py, pattern_analyzer.py, skill_generator.py

---

## Phase 5b — Canaux supplémentaires L'Aiguilleur (priorité moyenne)

### 5.1 `aiguilleur/rest/main.py` — REST API
- FastAPI, API Key via X-Api-Key header
- `/message` POST, `/docs` Swagger
- Consomme `relais:messages:outgoing:rest`

### 5.2 `aiguilleur/telegram/main.py`
- python-telegram-bot ≥ 21
- format_for_channel → MarkdownV2

### 5.3 `aiguilleur/slack/main.py`
- slack-bolt
- format_for_channel → mrkdwn

### 5.4 Canaux optionnels (priorité basse)
- `aiguilleur/matrix/main.py` — matrix-nio
- `aiguilleur/teams/main.py` — botbuilder-python
- `aiguilleur/whatsapp/index.js` — Baileys (Node.js)
- `aiguilleur/signal/run.sh` — signal-cli (Java)
- `aiguilleur/tui/main.py` — Textual (Le Tableau relay)

---

## Phase 6 — Architecture AIGUILLEUR configurable

### 6.1 ✅ Phase C — Refonte Souvenir DONE (2026-03-28)

Souvenir gère maintenant **deux streams** :

**Stream 1 : `relais:memory:request`** (consumer group existant)
- SUPPRIMER : action `append` (remplacée par observation outgoing)
- GARDER : action `get` → lire Redis List `relais:context:{session_id}` (cache) ; si vide → SQLite (fallback) → publier `relais:memory:response`

**Stream 2 : `relais:messages:outgoing:{channel}`** (nouveau consumer group)
- `_handle_outgoing(envelope)` :
  1. Lit `messages_raw` depuis `envelope.metadata["messages_raw"]` (blob sérialisé par Atelier via `serialize_messages()`)
  2. RPUSH blob complet → `relais:context:{session_id}`, LTRIM -20, EXPIRE 24h (un blob par tour)
  3. SQLite upsert sur `correlation_id` — champs `user_content` + `assistant_content` + `messages_raw JSON`
  4. `memory_extractor.extract()` → faits utilisateur → SQLite `user_facts` (fire-and-forget)

> ⚠️ Redis Streams n'a pas de wildcard consumer groups — Souvenir s'abonne aux canaux connus explicitement (liste depuis `config.yaml`)

**Flux `get` (Atelier → Souvenir → Atelier) :**
```
Atelier → XADD relais:memory:request  {action:"get", session_id, correlation_id}
Souvenir → LRANGE relais:context:{session_id} 0 19  (cache Redis)
           si [] → SELECT messages FROM sqlite WHERE session_id ORDER BY ts DESC LIMIT 20 (fallback)
        → XADD relais:memory:response {correlation_id, messages:[...]}
Atelier ← XREAD relais:memory:response (filtre correlation_id, timeout 3s)
```

**`souvenir/memory_extractor.py`** — IMPLÉMENTÉ :
- `MemoryExtractor(model="provider:model-id")` — ex: `MemoryExtractor(model="anthropic:claude-haiku-4-5")`
- `async def extract(envelope: Envelope) -> list[dict]`
- `init_chat_model(model, temperature=0.1, max_tokens=512)` + `ainvoke([SystemMessage, HumanMessage])`
- Filtre confidence > 0.7, fire-and-forget (non-bloquant)

### 6.2 ✅ Architecture AIGUILLEUR configurable (2026-03-30) DONE

`AiguilleurManager` charge les canaux depuis `channels.yaml` (enabled/disabled, streaming flag, type, restart policy). Adapter discovery par convention : `aiguilleur.channels.{name}.adapter` ou `class_path` override. Restart automatique avec backoff exponentiel.

---

## Phase 7 — Profil LLM par canal

**Objectif :** permettre à chaque canal de forcer un profil LLM spécifique, indépendamment des utilisateurs.

**Décision :** canal gagne toujours ; fallback sur `config.yaml > llm.default_profile` → `"default"`.

### 7.1 `aiguilleur/channel_config.py` — Ajout champ `profile`
- Ajouter `profile: str | None = None` dans `ChannelConfig` dataclass
- `load_channels_config()` parse le champ optionnel sans breaking change

**Tests** (`tests/test_channel_config.py`) :
- Canal avec `profile: fast` → `config.profile == "fast"`
- Canal sans `profile` → `config.profile is None`
- `load_channels_config()` gère les deux cas

### 7.2 Stamping dans les adaptateurs Aiguilleur
- Au moment de la création de chaque enveloppe entrante, l'adaptateur stampe `envelope.metadata["channel_profile"]`
- Si `channel_config.profile` est défini → utiliser cette valeur
- Sinon → `get_default_llm_profile()` depuis `common/config_loader.py`

**Tests** (`tests/test_discord_adapter.py`) :
- Canal avec `profile: fast` → `metadata["channel_profile"] == "fast"`
- Canal sans `profile` → `metadata["channel_profile"] == valeur config.yaml`

### 7.3 `common/config_loader.py` — `get_default_llm_profile()`
- Fonction helper qui lit `config.yaml > llm.default_profile`
- Retourne `"default"` si la clé est absente

**Tests** :
- Retourne la valeur de `config.yaml > llm.default_profile` quand présente
- Retourne `"default"` si clé absente

### 7.4 `sentinelle/acl.py` — Deprecation `get_effective_profile()`
- Ajouter docstring deprecation sur la méthode (ne pas supprimer)
- Note : cette méthode n'est plus dans le chemin de résolution du profil

---

## Phase 8 — Tests (continu)

| Type | Cible | Outil |
|------|-------|-------|
| Unit | common/ (envelope, config_loader) | pytest |
| Unit | chaque brique isolée | pytest + Redis mock |
| Integration | pipeline complet Discord → réponse | pytest + Redis réel |
| E2E | message Discord entrant + réponse | discord.py test client |

**Fichiers de test Atelier (migration DeepAgents) :**
- `tests/test_agent_executor.py` — remplace `test_sdk_executor.py` (mocks DeepAgents, streaming buffer, contrat XACK)
- `tests/test_tools.py` — `list_skills`, `read_skill`, path traversal guard
- `tests/test_mcp_adapter.py` — mock `McpSessionManager`, wrappers `_McpTool`, skip serveur en erreur

**Couverture cible:** 80% (règle commune)

---

## Résumé des gaps critiques

### Immédiatement nécessaires pour fiabilité production
1. ✅ `common/shutdown.py` — graceful shutdown propre (SIGTERM en production)
2. ✅ `common/stream_client.py` — factorisation consumer group (DRY)
3. ✅ `config/profiles.yaml.default` — L'Atelier charge les profils LLM
4. ✅ `config/users.yaml.default` — La Sentinelle a besoin des users pour ACL réelle
5. ✅ `soul/SOUL.md.default` — L'Atelier assemble le prompt avec SOUL

### Nécessaires pour le premier canal supplémentaire
6. ✅ `common/markdown_converter.py`
7. ✅ `aiguilleur/base.py`

### Nouvelles briques par ordre de valeur
8. `crieur/` — push proactif (notifications importantes)
9. `veilleur/` — tâches planifiées (heartbeat Benjamin)
10. `sentinelle/acl.py` — sécurité réelle (actuellement tout est autorisé)
11. `souvenir/long_term_store.py` — mémoire persistante (actuellement volatile Redis)
12. `aiguilleur/rest/` — canal REST/SSE + webhooks HMAC
13. `vigile/` — admin NLP + hot reload
14. `forgeron/` — apprentissage automatique
15. `scrutateur/` — monitoring
16. `tableau/` — interface admin TUI

---

## Dépendances à ajouter dans pyproject.toml

```toml
# Phase 2-3 (implémentées)
sqlmodel = ">=0.14"
alembic = ">=1.13"
pydantic = ">=2.9"
python-dotenv = ">=1.0"

# Atelier DeepAgents (implémentées)
deepagents        # moteur agentique LangGraph
langchain-core       # BaseTool, messages
langchain-openai     # init_chat_model support OpenAI-compatible providers (LM Studio, etc.)
langchain-openrouter # OpenRouter provider
langchain-ollama     # Ollama local provider
langchain-mistralai  # Mistral provider
langchain-deepseek   # DeepSeek provider

# Phase 4
apscheduler = ">=4.0"
fastapi = ">=0.115"
uvicorn = ">=0.30"
aiohttp = ">=3.9"

# Phase 5
python-telegram-bot = ">=21.0"
slack-bolt = ">=1.20"

# Phase 9
textual = ">=1.0"
prometheus-client = ">=0.20"
```

---

## Phase 9 — Interfaces d'administration (priorité basse)

### 9.1 `vigile/` — Admin NLP + hot reload
**Consomme:** `relais:admin:*` (Pub/Sub)
**Commandes NLP:** "redémarre l'atelier", "recharge la config", "active le mode vacances"
**Pilote:** supervisord via XML-RPC
**Hot reload:** publie `relais:admin:reload` → toutes briques
**Fichiers:** main.py, supervisord_client.py, nlp_parser.py

### 9.2 `tisserand/` — Intercepteurs in-process
**Taxonomie:** Interceptor Chain (dans L'Atelier)
**Pattern:** middleware chain pre/post LLM call
**Timeout:** 2s par intercepteur
**Fichiers:** main.py, events.py, extension_base.py

### 9.3 `tableau/` — TUI Textual bidirectionnel
**Taxonomie:** Admin + Relay
**Dépendance:** Textual ≥ 1.0
**Fichiers:** main.py, app.py, screens/, widgets/
**Priorité supervisord:** 30 (autostart=false)

### 9.4 `scrutateur/` — Monitoring Prometheus/Loki
**Taxonomie:** Pure Observer
**Souscrit:** `relais:events:*` (Pub/Sub)
**Expose:** /metrics (Prometheus)
**Optionnel:** Loki push, Elasticsearch
**Fichiers:** main.py, grafana/

---

## Phase 10 — Infrastructure MCP & extensions (priorité basse)

### 10.1 `mcp/calendar/server.py` — MCP Google Calendar
### 10.2 `mcp/brave-search/server.js` — MCP Brave Search
### 10.3 `extensions/` — Extensions natives (quota-enforcer, content-filter)
### 10.4 `observers/` — Observers out-of-process (examples Python/Node)

---

## Phases documentation ✅ FAIT

### Phase E — Tests Atelier (2026-03-30) ✅ FAIT

| Fichier | État | Notes |
|---------|------|-------|
| `tests/test_agent_executor.py` | ✅ CRÉÉ | Mock `create_deep_agent`, streaming buffer 80 chars, contrat XACK |
| `tests/test_tools.py` | ✅ CRÉÉ | `list_skills`, `read_skill`, path traversal guard |
| `tests/test_mcp_adapter.py` | ✅ CRÉÉ | Mock `McpSessionManager`, wrappers `_McpTool`, skip serveur en erreur |
| `tests/test_mcp_session_manager.py` | ✅ CRÉÉ | `call_tool` server not found, timeout, TimeoutError → string |
| `tests/test_stream_publisher.py` | ✅ | `seq` incrémenté, `is_final=1` sur `finalize()`, format clé Redis |

### Phase F — Documentation (2026-03-30) ✅ FAIT

| Fichier | État | Changements |
|---------|------|-------------|
| `CLAUDE.md` | ✅ MIS À JOUR | Section Atelier : `AgentExecutor`/`DeepAgents`, `ToolPolicy` (remplace `make_skills_tools()`), `McpSessionManager` ; dépendances `deepagents` |
| `docs/ARCHITECTURE.md` | ✅ MIS À JOUR | Section Atelier : `AgentExecutor` remplace `SDKExecutor`, diagramme DeepAgents, suppression mentions LiteLLM proxy |
| `plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md` | ✅ MIS À JOUR | Exigences fonctionnelles alignées ; détails impl Anthropic SDK et LiteLLM supprimés |

---

*Plan consolidé le 2026-03-31 — basé sur RELAIS_ARCHITECTURE_COMPLETE_v12.md et audit post-migration DeepAgents*
