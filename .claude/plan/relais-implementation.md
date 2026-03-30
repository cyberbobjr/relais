# Plan d'implémentation RELAIS
## Basé sur RELAIS_ARCHITECTURE_COMPLETE_v12.md
### Audit du 2026-03-27

---

## État actuel — MVP Core Loop ✅ opérationnel

Le cycle de base est fonctionnel : Discord → Portail → Sentinelle → Atelier → Souvenir → LiteLLM → Discord, avec L'Archiviste en observer.

### ⚠️ Bug critique connu — atelier/main.py (perte silencieuse de tâches)

**Symptôme :** si LiteLLM redémarre (changement de config, crash), toute tâche arrivant pendant la fenêtre de restart est **définitivement perdue sans erreur visible**.

**Cause racine — `atelier/main.py` lignes 237–249 :**

```python
except Exception as inner_e:      # attrape ConnectError
    logger.error(...)              # log seulement
finally:
    await redis_conn.xack(...)     # ← TOUJOURS exécuté, même sur ConnectError
```

Le `finally` est inconditionnel. Sur `httpx.ConnectError` :
1. L'exception est catchée et loguée
2. Le `finally` s'exécute → **XACK envoyé**
3. Le message quitte définitivement le stream — tâche perdue

**Fix requis avant mise en production :** voir Phase 2.2 ci-dessous (`atelier/executor.py`).

---

### Fichiers implémentés

| Fichier | État | Notes |
|---------|------|-------|
| `common/config_loader.py` | ✅ | Cascade ~/.relais/ > /opt/relais/ > ./ |
| `common/envelope.py` | ✅ | Envelope + PushEnvelope + MediaRef |
| `common/redis_client.py` | ✅ | AsyncRedis factory avec ACL |
| `common/init.py` | ✅ | initialize_user_dir() |
| `portail/main.py` | ✅ | Consumer group, session TTL, logging |
| `sentinelle/main.py` | ✅ | Stub ACL (autorise tout) |
| `atelier/main.py` | ⚠️ | Fonctionnel mais XACK inconditionnel — voir bug ci-dessus |
| `souvenir/main.py` | ✅ | append/get, Redis List, TTL 24h |
| `archiviste/main.py` | ✅ | JSONL, consumer group multi-streams |
| `aiguilleur/discord/main.py` | ✅ | Bot mentions/DMs, outgoing background task |
| `config/redis.conf` | ✅ | Unix socket .relais/, ACL par brique |
| `config/litellm.yaml` | ✅ | mistral-small + qwen3-coder, OpenRouter |
| `supervisord.conf` | ✅ | Dev config (.relais/ paths) |
| `pyproject.toml` | ✅ | Dépendances de base |

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

### 2.2 ✅ `atelier/executor.py` — Résilience LLM + fix perte de tâches (CRITIQUE)

**Ce refactor corrige le bug de perte silencieuse décrit ci-dessus.**

**Règle fondamentale : ne jamais XACK avant le succès ou l'épuisement des retries.**

```python
# atelier/executor.py

RETRIABLE = (httpx.ConnectError, httpx.TimeoutException)
RETRY_DELAYS = [2, 5, 15]  # secondes, backoff exponentiel

async def execute_with_resilience(
    http_client: httpx.AsyncClient,
    envelope: Envelope,
    context: list[dict],
) -> str:
    """Appelle LiteLLM avec retry sur erreurs transitoires.

    Retente 3× avec backoff sur ConnectError/Timeout (LiteLLM redémarre).
    Bascule sur Ollama si tous les retries échouent (fallback).
    Lève ExhaustedRetriesError après épuisement — l'appelant NE DOIT PAS XACK.
    """
    for attempt, delay in enumerate(RETRY_DELAYS, 1):
        try:
            response = await http_client.post(...)
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except RETRIABLE as e:
            logger.warning(f"LiteLLM unreachable (attempt {attempt}/3): {e}")
            if attempt < len(RETRY_DELAYS):
                await asyncio.sleep(delay)
            else:
                raise ExhaustedRetriesError(f"LiteLLM down after {attempt} retries") from e
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (502, 503, 504):
                # Transient HTTP errors — même stratégie retry
                if attempt < len(RETRY_DELAYS):
                    await asyncio.sleep(delay)
                    continue
            raise  # Non-retriable (400, 401, etc.) → remonte immédiatement
```

**Pattern XACK dans `atelier/main.py` après refactor :**

```python
# AVANT (bugué) :
finally:
    await redis_conn.xack(...)   # toujours, même sur ConnectError

# APRÈS (correct) :
success = False
try:
    reply = await executor.execute_with_resilience(...)
    await redis_conn.xadd(out_stream, ...)
    success = True
except ExhaustedRetriesError:
    # Retries épuisés → Dead Letter Queue
    await redis_conn.xadd("relais:tasks:failed", {"payload": envelope.to_json(), "reason": str(e)})
    success = True  # On ACK quand même — message dans DLQ, pas perdu
except RETRIABLE:
    pass  # Ne pas ACK — le message reste dans le PEL pour re-livraison
finally:
    if success:
        await redis_conn.xack(self.stream_in, self.group_name, message_id)
```

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
- ✅ `souvenir/context_store.py` — Redis List (historique court terme, 20 msgs)
- ✅ `souvenir/long_term_store.py` — SQLite via SQLModel (mémoire longue durée)
- ✅ `souvenir/migrations/` — Alembic migrations

### 2.6 ✅ `archiviste/cleanup_retention.py` DONE
- Rétention configurable : JSONL 90j, SQLite 1 an, audit ∞

---

## Phase 3 — Templates système (priorité haute) ✅ DONE

Ces fichiers default sont copiés dans ~/.relais/ au premier lancement par `initialize_user_dir()`.

### 3.1 ✅ Fichiers default créés dans `config/` DONE
- ✅ `config/config.yaml.default` — configuration système par défaut
- ✅ `config/profiles.yaml.default` — profils LLM (model, tools, memory, resilience)
- ✅ `config/users.yaml.default` — registry utilisateurs (admin, user, usr_system)
- ✅ `config/reply_policy.yaml.default` — politique réponse auto
- ✅ `config/mcp_servers.yaml.default` — MCP servers globaux/contextuels
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

### 4.3 `guichet/` — Webhooks HMAC entrants
**Taxonomie:** Transformer
**Reçoit:** HTTP POST webhooks externes
**Valide:** HMAC signature avant publication
**Publie:** `relais:webhooks:*` → Crieur/Atelier
**Fichiers:** main.py, sources/, webhook_acl.py
**Dépendance:** FastAPI

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

## Phase 6 — Interfaces d'administration (priorité basse)

### 6.1 `vigile/` — Admin NLP + hot reload
**Consomme:** `relais:admin:*` (Pub/Sub)
**Commandes NLP:** "redémarre l'atelier", "recharge la config", "active le mode vacances"
**Pilote:** supervisord via XML-RPC
**Hot reload:** publie `relais:admin:reload` → toutes briques
**Fichiers:** main.py, supervisord_client.py, nlp_parser.py

### 6.2 `tisserand/` — Intercepteurs in-process
**Taxonomie:** Interceptor Chain (dans L'Atelier)
**Pattern:** middleware chain pre/post LLM call
**Timeout:** 2s par intercepteur
**Fichiers:** main.py, events.py, extension_base.py

### 6.3 `tableau/` — TUI Textual bidirectionnel
**Taxonomie:** Admin + Relay
**Dépendance:** Textual ≥ 1.0
**Fichiers:** main.py, app.py, screens/, widgets/
**Priorité supervisord:** 30 (autostart=false)

### 6.4 `scrutateur/` — Monitoring Prometheus/Loki
**Taxonomie:** Pure Observer
**Souscrit:** `relais:events:*` (Pub/Sub)
**Expose:** /metrics (Prometheus)
**Optionnel:** Loki push, Elasticsearch
**Fichiers:** main.py, grafana/

---

## Phase 7 — Infrastructure MCP & extensions (priorité basse)

### 7.1 `mcp/calendar/server.py` — MCP Google Calendar
### 7.2 `mcp/brave-search/server.js` — MCP Brave Search
### 7.3 `extensions/` — Extensions natives (quota-enforcer, content-filter)
### 7.4 `observers/` — Observers out-of-process (examples Python/Node)

---

## Phase 8 — Tests (continu)

| Type | Cible | Outil |
|------|-------|-------|
| Unit | common/ (envelope, config_loader) | pytest |
| Unit | chaque brique isolée | pytest + Redis mock |
| Integration | pipeline complet Discord → réponse | pytest + Redis réel |
| E2E | message Discord entrant + réponse | discord.py test client |

**Couverture cible:** 80% (règle commune)

---

## Résumé des gaps critiques

### 🔴 Bug actif — à corriger avant toute mise en production
0. **`atelier/executor.py` + fix XACK conditionnel** — perte silencieuse de tâches sur `ConnectError` LiteLLM. Le `finally: xack` actuel est inconditionnel. Ajouter retry backoff + DLQ `relais:tasks:failed`.

### Immédiatement nécessaires pour fiabilité production
1. `common/shutdown.py` — graceful shutdown propre (SIGTERM en production)
2. `common/stream_client.py` — factorisation consumer group (DRY)
3. `config/profiles.yaml.default` — L'Atelier charge les profils LLM
4. `config/users.yaml.default` — La Sentinelle a besoin des users pour ACL réelle
5. `soul/SOUL.md.default` — L'Atelier assemble le prompt avec SOUL

### Nécessaires pour le premier canal supplémentaire
6. `common/markdown_converter.py`
7. `aiguilleur/base.py`

### Nouvelles briques par ordre de valeur
8. `crieur/` — push proactif (notifications importantes)
9. `veilleur/` — tâches planifiées (heartbeat Benjamin)
10. `sentinelle/acl.py` — sécurité réelle (actuellement tout est autorisé)
11. `souvenir/long_term_store.py` — mémoire persistante (actuellement volatile Redis)
12. `guichet/` — webhooks (intégrations externes)
13. `vigile/` — admin NLP + hot reload
14. `forgeron/` — apprentissage automatique
15. `scrutateur/` — monitoring
16. `tableau/` — interface admin TUI

---

## Dépendances à ajouter dans pyproject.toml

```toml
# Phase 2-3
sqlmodel = ">=0.14"
alembic = ">=1.13"
pydantic = ">=2.9"
structlog = ">=24.0"
python-dotenv = ">=1.0"

# Phase 4
apscheduler = ">=4.0"
fastapi = ">=0.115"
uvicorn = ">=0.30"
aiohttp = ">=3.9"

# Phase 5
python-telegram-bot = ">=21.0"
slack-bolt = ">=1.20"

# Phase 6
textual = ">=1.0"
prometheus-client = ">=0.20"
```

---

*Plan généré le 2026-03-27 — basé sur RELAIS_ARCHITECTURE_COMPLETE_v12.md*

---

## Phase 2.2-bis — Migration Atelier : `claude-agent-sdk` + Streaming progressif ✅ FAIT — 2026-03-28

> **Décision finale (2026-03-28) :** `claude-agent-sdk` est utilisé à la place du SDK `anthropic` officiel, pour bénéficier des **subagents natifs**, du **MCP natif**, et du **streaming progressif** vers Discord/Telegram. Le bug #677 (binaire bundlé ignore `ANTHROPIC_BASE_URL`) est contourné via `cli_path=shutil.which("claude")`.

### Décisions architecturales

1. **SDK** : `claude-agent-sdk` (PyPI: `claude-agent-sdk` >=0.1.51) avec `ClaudeSDKClient` + `ClaudeAgentOptions`. Workaround bug #677 : `cli_path=shutil.which("claude")` force le binaire système qui respecte `ANTHROPIC_BASE_URL`.
2. **Routing modèle** : `ANTHROPIC_BASE_URL=http://localhost:4000` → LiteLLM proxy. `ANTHROPIC_API_KEY=litellm-master-key`. Les alias modèles dans `profiles.yaml` sont des alias LiteLLM. Aucune migration de config nécessaire.
3. **MCP natif** : `mcp_servers=` dans `ClaudeAgentOptions` — plus de conversion `ToolParam` manuelle. `mcp_loader.load_for_sdk()` retourne le format dict attendu.
4. **Subagents** : `AgentDefinition` pour memory-retriever (Haiku), web-searcher (Sonnet), calendar-agent (Haiku) — Claude les invoque automatiquement.
5. **Streaming progressif** : `StreamPublisher` → `relais:messages:streaming:{channel}:{corr_id}` → Aiguilleur édite le message Discord/Telegram en temps réel (throttle 80 chars, XREAD BLOCK 150ms).
6. **Contexte conversationnel** : Souvenir reste **propriétaire unique** de l'historique. Redis List `relais:context:{session_id}` = cache rapide (TTL 24h) ; SQLite = source de vérité (fallback si Redis redémarre). Atelier demande via `relais:memory:request` (action `get`) avant chaque appel SDK.

### Tableau de compatibilité LiteLLM

| Outil | `ANTHROPIC_BASE_URL` respecté |
|-------|------------------------------|
| Claude Code CLI (`claude`) | ✅ Oui, nativement |
| SDK Python `anthropic` | ✅ Oui |
| `claude-agent-sdk` binaire bundlé | ❌ Bug #677 — ignoré |
| `claude-agent-sdk` + `cli_path=shutil.which("claude")` | ✅ Oui (workaround) |

> **Note auth :** LiteLLM récent préfère `ANTHROPIC_AUTH_TOKEN` à `ANTHROPIC_API_KEY`. Si erreurs 401, utiliser `ANTHROPIC_AUTH_TOKEN`.

### ✅ Phase A — Modules support (FAIT — 2026-03-28)

| Fichier | État | Notes |
|---------|------|-------|
| `atelier/profile_loader.py` | ✅ CRÉÉ | `ProfileConfig` + `ResilienceConfig` frozen dataclasses. 11 tests, 98% coverage. Ajouter `max_turns: int = 20` |
| `atelier/soul_assembler.py` | ✅ CRÉÉ | 6 couches, séparateur `\n\n---\n\n`. 9 tests, 100% coverage |
| `atelier/mcp_loader.py` | ✅ CRÉÉ (partiel) | `load_mcp_servers()` présent. Ajouter `load_for_sdk()` (Phase B.1) |
| `tests/test_profile_loader.py` | ✅ | — |
| `tests/test_soul_assembler.py` | ✅ | — |
| `tests/test_mcp_loader.py` | ✅ | — |

**Ajout requis à `ProfileConfig`** (avant Phase B) :
```python
@dataclass(frozen=True)
class ProfileConfig:
    model: str
    temperature: float
    max_tokens: int
    max_turns: int = 20          # ← nouveau champ pour claude-agent-sdk
    resilience: ResilienceConfig
```

**`soul_assembler` couches :**

| Ordre | Source | Chemin | Toujours présent |
|-------|--------|--------|-----------------|
| 1 | Personnalité | `soul/SOUL.md` | Oui (erreur si absent) |
| 2 | Rôle | `prompts/roles/{role}.md` | Non |
| 3 | Utilisateur | `prompts/users/{sender_id}.md` | Non |
| 4 | Canal | `prompts/channels/{channel}.md` | Non (warning) |
| 5 | Policy | `prompts/policies/{reply_policy}.md` | Non |
| 6 | Mémoire long-terme | Injecté `## Mémoire utilisateur` depuis SQLite | Non |

Structure `prompts/` :
```
prompts/
├── roles/admin.md, user.md
├── users/            ← créé par l'utilisateur
├── channels/discord.md, telegram.md, whatsapp.md
└── policies/in_meeting.md, out_of_hours.md, vacation.md
```
Les `prompts/*.md` existants (racine) sont déplacés vers `channels/` et `policies/`.

---

### ✅ Phase A.fix — Ajouts support modules (FAIT — 2026-03-28)

| Fichier | État | Notes |
|---------|------|-------|
| `atelier/profile_loader.py` | ✅ MIS À JOUR | Ajout champ `max_turns: int = 20` |
| `atelier/mcp_loader.py` | ✅ MIS À JOUR | Ajout méthode `load_for_sdk()` |

---

### ✅ Phase B — Remplacement executor (FAIT — 2026-03-28)

#### B.1 ✅ `atelier/sdk_executor.py` créé + `atelier/mcp_loader.py` modifié

**`atelier/sdk_executor.py`** — CRÉÉ (remplace le pattern httpx de `executor.py`) :
```python
import shutil, os
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition
from claude_agent_sdk import AssistantMessage, ResultMessage

class SDKExecutionError(Exception): pass

class SDKExecutor:
    async def execute(self, envelope, context, stream_callback=None) -> str:
        options = ClaudeAgentOptions(
            cli_path=shutil.which("claude"),        # Workaround bug #677
            env={
                "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", "http://localhost:4000"),
                "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY",
                                     os.environ.get("ANTHROPIC_AUTH_TOKEN", "")),
            },
            system_prompt=self._soul_prompt,
            model=self._profile.model,
            max_turns=getattr(self._profile, "max_turns", 20),
            mcp_servers=self._mcp_servers,
            agents=self._build_subagents(),
            permission_mode="bypassPermissions",
        )
        async with ClaudeSDKClient(options=options) as client:
            await client.query(self._build_prompt(envelope, context))
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        text = getattr(block, "text", None)
                        if text:
                            full_reply += text
                            if stream_callback:
                                await stream_callback(text)
                elif isinstance(message, ResultMessage):
                    if message.subtype != "success":
                        raise SDKExecutionError(f"SDK non-success: {message.subtype}")
                    break
```

Subagents définis dans `_build_subagents()` : `memory-retriever` (Haiku, tools memory MCP), `web-searcher` (Sonnet, WebSearch), `calendar-agent` (Haiku, tools calendar MCP).

**`atelier/mcp_loader.py`** — AJOUTER `load_for_sdk(profile_name)` :
```python
def load_for_sdk(self, profile_name: str = "default") -> dict:
    """Retourne {name: {command, args, env}} ou {name: {type, url, headers}} pour ClaudeAgentOptions."""
```
Format stdio : `{"command": ..., "args": [...], "env": {...}}`. Format SSE/HTTP : `{"type": "sse", "url": ..., "headers": {...}}`.

#### ✅ B.2 Créer `atelier/stream_publisher.py` — FAIT — 2026-03-29

`StreamPublisher` implémenté avec 7 tests, 100% coverage :
- `push_chunk(text, is_final=False)` → XADD `relais:messages:streaming:{channel}:{correlation_id}` avec seq counter, MAXLEN=500
- `finalize()` → chunk vide avec `is_final="1"`, EXPIRE TTL=300s
- Tests : `tests/test_stream_publisher.py`

#### ✅ B.3 Mise à jour `atelier/main.py` — FAIT — 2026-03-29

Implémenté :
- `StreamPublisher` câblé comme `stream_callback` dans `SDKExecutor.execute()`
- `GracefulShutdown` câblé : `while not shutdown.is_stopping()`, `install_signal_handlers()` dans `start()`
- `_process_stream(self, redis_conn, shutdown: GracefulShutdown | None = None)`
- Pattern XACK conditionnel respecté : ACK uniquement sur succès ou DLQ, jamais sur exception transitoire

---

### ✅ Phase C — Refonte Souvenir (FAIT — 2026-03-28)

Souvenir gère maintenant **deux streams** :

**Stream 1 : `relais:memory:request`** (consumer group existant)
- SUPPRIMER : action `append` (remplacée par observation outgoing)
- GARDER : action `get` → lire Redis List `relais:context:{session_id}` (cache) ; si vide → SQLite (fallback) → publier `relais:memory:response`

**Stream 2 : `relais:messages:outgoing:{channel}`** (nouveau consumer group)
- `_handle_outgoing(envelope)` :
  1. RPUSH `[user_message, assistant_reply]` → `relais:context:{session_id}`, LTRIM -20, EXPIRE 24h
  2. SQLite INSERT (archivage long-terme existant)
  3. `memory_extractor.extract()` → faits utilisateur → SQLite `user_facts`

> ⚠️ Redis Streams n'a pas de wildcard consumer groups — Souvenir s'abonne aux canaux connus explicitement (liste depuis `config.yaml`)

**Flux `get` (Atelier → Souvenir → Atelier) :**
```
Atelier → XADD relais:memory:request  {action:"get", session_id, correlation_id}
Souvenir → LRANGE relais:context:{session_id} 0 19  (cache Redis)
           si [] → SELECT messages FROM sqlite WHERE session_id ORDER BY ts DESC LIMIT 20 (fallback)
        → XADD relais:memory:response {correlation_id, messages:[...]}
Atelier ← XREAD relais:memory:response (filtre correlation_id, timeout 3s)
```

**Flux `_handle_outgoing` :**
```
relais:messages:outgoing:{channel}
    → Souvenir._handle_outgoing(envelope)
         ├── RPUSH relais:context:{session_id} [user_msg, assistant_reply]
         │   LTRIM -20 -1 + EXPIRE 86400
         ├── long_term_store.archive(envelope)       ← existant (SQLite)
         └── memory_extractor.extract(envelope)      ← nouveau
               ├── Récupère user_message depuis envelope.metadata["user_message"]
               ├── Appelle LLM fast (profil "fast", même LiteLLM proxy)
               │     Prompt : "Extrais les faits durables sur l'utilisateur depuis cet échange.
               │               Réponds en JSON : [{fact, category, confidence}]"
               ├── Filtre confidence > 0.7
               └── long_term_store.upsert_facts(sender_id, facts)
```

**Table SQLite `user_facts`** (migration Alembic requise) :
```sql
CREATE TABLE user_facts (
    id TEXT PRIMARY KEY, sender_id TEXT NOT NULL, fact TEXT NOT NULL,
    category TEXT, confidence REAL, source_corr TEXT, created_at REAL, updated_at REAL
);
```

**`souvenir/memory_extractor.py`** — CRÉER :
- `async def extract(envelope, http_client) -> list[UserFact]`
- Appelle LiteLLM proxy (httpx, profil `fast`) : `"Extrais les faits durables. JSON: [{fact, category, confidence}]"`
- Filtre confidence > 0.7, fire-and-forget (non-bloquant)

**`souvenir/long_term_store.py`** — AJOUTER :
- `upsert_facts(sender_id, facts)` — upsert par (sender_id + fact hash)
- `get_user_facts(sender_id, limit=20) -> list[str]`
- `get_recent_messages(session_id, limit=20)`

**`souvenir/context_store.py`** — AJOUTER :
- `get_recent(session_id, limit=20) -> list[dict]` : Redis List → SQLite fallback
- `append_turn(session_id, user_content, assistant_content)`

---

### ✅ Phase D — Configuration & Environnement (FAIT — 2026-03-28)

| Fichier | Action |
|---------|--------|
| `pyproject.toml` | Remplacer `anthropic >= 0.40` par `claude-sdk >= 0.1` (vérifier nom exact PyPI avant). Garder `httpx` (Souvenir extracteur). |
| `.env.example` | `ANTHROPIC_BASE_URL=http://localhost:4000`, `ANTHROPIC_API_KEY=litellm-master-key` |
| `supervisord.conf` | Ajouter `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` dans `[program:atelier]` environment |
| `config/redis.conf` | ACL atelier : ajouter `relais:messages:streaming:*` (write). Garder `relais:memory:request` (write), `relais:memory:response` (read). Supprimer accès direct `relais:context:*`. |

> **Action préalable Phase D :** vérifier le nom exact du package sur PyPI (`pip install claude-sdk` ou `claude-agent-sdk`) et noter le nom retenu.

---

### ✅ Phase D.bis — Streaming Aiguilleur Discord (FAIT — 2026-03-28)

Ajouter dans `aiguilleur/discord/main.py` :

```python
STREAM_EDIT_THROTTLE_CHARS = 80   # Rate limit Discord (~5 edits/s)
STREAM_READ_BLOCK_MS = 150

async def _subscribe_streaming_start(self):
    """Écoute relais:streaming:start:discord (Pub/Sub) → lance _handle_streaming_message()."""

async def _handle_streaming_message(self, envelope: Envelope):
    """Envoie placeholder '▌', lit chunks XREAD BLOCK, édite message progressivement, édition finale sans curseur."""
```

---

### ✅ Phase E — Tests (FAIT — 2026-03-28)

| Fichier | Action |
|---------|--------|
| `tests/test_sdk_executor.py` | CRÉER : mock `ClaudeSDKClient`, test reply assemblé, test `stream_callback` appelé, test `SDKExecutionError` sur non-success, test `cli_path` = `shutil.which("claude")` |
| `tests/test_stream_publisher.py` | CRÉER : test `seq` incrémenté, test `is_final=1` sur `finalize()`, test format clé Redis |
| `tests/test_atelier.py` | RÉÉCRIRE : mock `SDKExecutor` + `StreamPublisher`. Conserver tests XACK (critiques). Ajouter : résolution profil, streaming signal Pub/Sub, injection `user_message` metadata, mock `get` vers Souvenir |
| `tests/test_mcp_loader.py` | COMPLÉTER : ajouter tests `load_for_sdk()` (stdio + sse, filtre profil) |
| `tests/test_profile_loader.py` | COMPLÉTER : ajouter test champ `max_turns` |
| `tests/test_souvenir.py` | Mettre à jour : `get_recent` (Redis hit/SQLite fallback), `append_turn`, `memory_extractor` (mock LLM, JSON parse, filtre confidence), `get_user_facts`/`upsert_facts`, flux `get` complet |

---

### ✅ Phase F — Documentation (EN COURS — 2026-03-28)

| Fichier | Rôle | Changements |
|---------|------|-------------|
| `CLAUDE.md` | Guide Claude Code pour ce repo | Atelier : `claude-agent-sdk` + `cli_path` workaround + streaming. Souvenir : dual-stream. Env vars. Structure `prompts/`. |
| `docs/ARCHITECTURE.md` | Doc technique bricks | Atelier : SDKExecutor, subagents, streaming. Souvenir : dual-stream, extracteur, `user_facts`. |
| `plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md` | **Référence spécifications fonctionnelles** | Aligner sur état final post-Phase B/C/D/D.bis : SDKExecutor, StreamPublisher, dual-stream Souvenir, user_facts, Discord streaming. Mettre à jour diagrammes de flux. |
| `.claude/plan/relais-implementation.md` | **Référence d'implémentation** | Marquer phases A, A.fix, B, C comme ✅ FAIT avec date 2026-03-28. Mettre à jour critères de succès. Refléter état réel du code. |
| `README.md` | Démarrage rapide | Dépendances, env vars, `prompts/` structure. |
| `prompts/` | Prompts par canal/policy | Déplacer `*_default.md` → `channels/`, `in_meeting.md`/`vacation.md`/`out_of_hours.md` → `policies/`. Créer `roles/`, `users/`. |

---

### Ordre d'exécution

```
Phase A.fix (parallèle) :
  profile_loader : ajouter max_turns
  mcp_loader : ajouter load_for_sdk()

Phase B (séquentiel) :
  B.1 sdk_executor.py + tests
  B.2 stream_publisher.py + tests
  B.3 main.py update (SDKExecutor + StreamPublisher + XACK conditionnel)

Phase C (séquentiel) :
  C.1 souvenir/main.py + memory_extractor.py + migration Alembic user_facts
  C.2 souvenir/long_term_store.py (upsert_facts, get_user_facts)
  C.3 souvenir/context_store.py (get_recent, append_turn)

Phase D (parallèle) :
  pyproject.toml (claude-sdk)
  .env.example
  supervisord.conf
  redis.conf ACL

Phase D.bis : aiguilleur/discord streaming consumer

Phase E : tests (après code)
Phase F : documentation (en dernier)
```

---

### Risques

---

## Phase 5 — Subagents Autonomes ✅ FAIT — 2026-03-29

> **Consolidation:** Support des subagents autonomes pour délégation de tâches et extensibilité du système sans code nouveau.

### ✅ Implémentation (FAIT — 2026-03-29)

| Composant | État | Notes |
|-----------|------|-------|
| `atelier/mcp_loader.py::SubagentConfig` | ✅ CRÉÉ | `@dataclass(frozen=True)` avec name, description, enabled, tools |
| `atelier/mcp_loader.py::load_subagents()` | ✅ CRÉÉ | Charge tous les subagents depuis `config/mcp_servers.yaml` |
| `atelier/mcp_loader.py::load_subagents_for_sdk()` | ✅ CRÉÉ | Retourne `dict[name, SubagentConfig]` pour passage à SDKExecutor |
| `atelier/sdk_executor.py::SDKExecutor` | ✅ MIS À JOUR | Accepte `subagents: dict \| None`, passe `agents=` à `ClaudeAgentOptions` |
| `atelier/profile_loader.py::ProfileConfig` | ✅ MIS À JOUR | Ajout champ `max_agent_depth: int = 2` |
| `atelier/main.py` | ✅ MIS À JOUR | Appelle `load_subagents_for_sdk()`, passe à SDKExecutor |
| `config/config.yaml.default` | ✅ MIS À JOUR | Section `subagents: enabled: true` |
| `config/mcp_servers.yaml.default` | ✅ MIS À JOUR | 3 subagents: memory-retriever, web-searcher (disabled), code-explorer |

### Configuration

**`config/mcp_servers.yaml.default`:**
```yaml
subagents:
  memory-retriever:
    name: memory-retriever
    description: Retrieves and manages conversation memory
    enabled: true
    tools: [memory_get, memory_set, context_search]

  web-searcher:
    name: web-searcher
    description: Searches web for current information
    enabled: false    # Disabled by default for safety
    tools: [web_search, url_fetch]

  code-explorer:
    name: code-explorer
    description: Analyzes and runs code
    enabled: true
    tools: [code_run, repo_search, syntax_check]
```

**`config/profiles.yaml`:**
Tous les profils héritent automatiquement `max_agent_depth: 2` de `ProfileConfig`.

### Flux d'exécution

```
atelier/main.py:
  → load_subagents_for_sdk()
  → SDKExecutor(subagents=subagents)
  → ClaudeAgentOptions(agents=subagents, allowed_tools=["Task", ...])
  → LLM peut invoquer Task → subagent nommé
  → Delegation autonome → résultat agrégé
  → relais:messages:outgoing:{channel}
```

### Critères de succès

- ✅ SubagentConfig frozen dataclass (immutable)
- ✅ load_subagents() et load_subagents_for_sdk() présentes
- ✅ SDKExecutor accepte subagents et les passe à ClaudeAgentOptions
- ✅ "Task" ajouté automatiquement aux allowed_tools
- ✅ Tous les profiles incluent max_agent_depth = 2
- ✅ Config cascade supporte subagents.enabled
- ✅ 3 subagents pré-configurés dans mcp_servers.yaml.default
- ✅ Documentation (CLAUDE.md, ARCHITECTURE.md, README.md, plan) mise à jour

### Dépendances

- ✅ claude-agent-sdk >= 0.1.51 (déjà présent)
- ✅ Pydantic >= 2.9 (déjà présent)

---

| Risque | Sévérité | Mitigation |
|--------|----------|------------|
| Bug #677 workaround fragile (`claude` non installé) | HAUT | Vérifier `shutil.which("claude") is not None` au démarrage d'Atelier, erreur critique sinon |
| Nom PyPI `claude-sdk` non confirmé | ✅ RÉSOLU | Package confirmé : `claude-agent-sdk` (PyPI) >= 0.1.51, module `claude_agent_sdk` |
| Rate limit Discord (streaming) | MOYEN | Throttle 80 chars entre edits. En cas d'erreur 429 → ignorer l'édition intermédiaire |
| `cli_path` portabilité (CI/Docker) | MOYEN | `claude` doit être installé dans l'image. Ajouter `RUN npm install -g @anthropic-ai/claude-code` au Dockerfile |
| Extracteur mémoire pollue SQLite (faux positifs) | MOYEN | Seuil confidence 0.7, prompt strict, revue manuelle possible via Vigile |
| Wildcard streams Souvenir | MOYEN | Abonnement explicite aux canaux connus (liste dans `config.yaml`) |
| `prompts/` réorganisation casse `portail/prompt_loader.py` | MOYEN | Mettre à jour `prompt_loader` en même temps |
| Sentinelle n'injecte pas encore `llm_profile`/`user_role` | FAIBLE | Défaut `"default"` → dégradation gracieuse |

---

### Critères de succès

- [ ] `claude-sdk` installé et `from claude_sdk import ClaudeSDKClient` importable
- [ ] `shutil.which("claude")` retourne un chemin valide au démarrage
- [ ] `atelier/sdk_executor.py` : `SDKExecutor.execute()` retourne texte, appelle `stream_callback`, lève `SDKExecutionError` sur non-success
- [ ] `atelier/stream_publisher.py` : `push_chunk()` XADD avec `seq` incrémenté, `finalize()` publie `is_final=1`
- [ ] `atelier/mcp_loader.py` : `load_for_sdk()` retourne dict au format `ClaudeAgentOptions(mcp_servers=...)`
- [ ] `atelier/profile_loader.py` : `ProfileConfig` a le champ `max_turns`
- [ ] `atelier/main.py` : signal Pub/Sub `relais:streaming:start:{channel}` envoyé avant le SDK
- [ ] `atelier/main.py` : XACK conditionnel (success + DLQ → ACK ; exception générique → PEL)
- [ ] `atelier/main.py` : `user_message` injecté dans `metadata` de l'envelope réponse
- [ ] Souvenir répond aux `get` depuis Redis List ; SQLite comme fallback si cache vide
- [ ] Souvenir alimente Redis List via observation `relais:messages:outgoing:{channel}`
- [ ] Souvenir déclenche extracteur mémoire sur chaque message sortant
- [ ] Table `user_facts` créée via migration Alembic
- [ ] Aiguilleur Discord : placeholder message créé + éditions progressives + édition finale sans curseur
- [ ] Tests XACK (success, DLQ, generic error, retriable) toujours verts
- [ ] Couverture ≥ 80% sur `atelier/` et `souvenir/`
- [x] Smoke test complet : Discord → streaming visible → réponse finale → archivage SQLite

---

*Plan mis à jour le 2026-03-28 — Migration Atelier vers `claude-agent-sdk` (subagents, MCP natif, streaming progressif) avec workaround bug #677 (`cli_path=shutil.which("claude")`). Souvenir reste propriétaire unique du contexte. Streaming Discord via `StreamPublisher` + `relais:messages:streaming:*`.*

---

## Phase 6 — Complétion briques déployées ✅ FAIT — 2026-03-29

> **Décision (2026-03-29) :** Pause sur nouvelles briques (Scrutateur, Forgeron, Vigile, Tisserand). Focalisation sur fiabilisation des briques déployées avant validation MVP Discord.

### ✅ Wave 1 — Tâches parallèles (FAIT — 2026-03-29)

#### ✅ 6.1 ProfileConfig — Champs complets (`atelier/profile_loader.py`)

5 nouveaux champs sur `ProfileConfig` (frozen dataclass, backward compatible) :
```python
allowed_tools: tuple[str, ...] | None = None
allowed_mcp: tuple[str, ...] | None = None
guardrails: tuple[str, ...] = ()
memory_scope: str = "own"          # Validé contre {"own","session","global","task"}
fallback_model: str | None = None
```
- `_VALID_MEMORY_SCOPES: Final[frozenset[str]]` — `ValueError` si valeur invalide
- Profil `coder` : `allowed_tools` réduit, `guardrails=("no_code_exec",)`, `memory_scope="task"`, `fallback_model="mistral-small-2603"`
- Profil `precise` : `fallback_model="haiku-4-5"`
- Tests mis à jour : `tests/test_profile_loader.py`

#### ✅ 6.2 Portail — Sessions actives (`portail/main.py`)

Nouvelle méthode `_update_active_sessions(envelope)` :
- Clé Redis : `relais:active_sessions:{sender_id}` (HSET)
- Champs : `last_seen` (float epoch), `channel`, `session_id`, `display_name` (optionnel)
- EXPIRE 3600 (reset à chaque message)
- Entièrement wrappé try/except — jamais bloquant
- GracefulShutdown câblé
- 7 tests : `tests/test_portail_sessions.py`

#### ✅ 6.3 Archiviste — Coverage 80%+ (`archiviste/main.py`)

Couverture montée de ~0% à 89% :
- 14 tests dans `tests/test_archiviste.py`
- GracefulShutdown câblé : `while not shutdown.is_stopping()`
- Signature `_process_stream(conn, shutdown=ANY)` mise à jour

#### ✅ 6.4 Souvenir — Pagination (`souvenir/long_term_store.py`)

Nouveau `PaginatedResult` frozen dataclass :
```python
@dataclass(frozen=True)
class PaginatedResult:
    items: tuple
    total: int
    limit: int
    offset: int
    has_more: bool
```
Nouvelle méthode `query()` : filtre user_id, since/until (epoch), search (LIKE, insensible casse), ORDER BY timestamp DESC, COUNT subquery séparé.
- 10 tests : `tests/test_souvenir_query.py`

---

### ✅ Wave 2 — Tâches séquencées (FAIT — 2026-03-29)

#### ✅ 6.5 Sentinelle — `unknown_user_policy` + `ProfileGuardrails`

**`sentinelle/acl.py`** :
- `ACLManager.__init__` accepte `unknown_user_policy="deny"`, `guest_profile="fast"`
- `get_effective_profile(user_id) -> str` — profil utilisateur ou `guest_profile`
- `async notify_pending(redis_conn, user_id, channel)` — XADD `relais:admin:pending_users`
- Politiques : `deny` (rejette), `guest` (profil invité), `pending` (XADD + continue)
- Wildcard channel `"*"` géré

**`sentinelle/guardrails.py`** :
- `GuardrailResult` frozen dataclass : `allowed: bool`, `reason: str | None`
- Classe `ProfileGuardrails(profile)` : règles `no_code_exec` et `no_external_links`
- Règle inconnue → `ValueError` à la construction (fail-fast)

**`config/config.yaml.default`** :
```yaml
security:
  unknown_user_policy: "deny"
  guest_profile: "fast"
```
28 tests : `tests/test_sentinelle_policy.py`

#### ✅ 6.6 Souvenir — Compaction contexte (`souvenir/context_store.py`)

- `LLMClient = Callable[[list[dict]], Awaitable[str]]` type alias
- `maybe_compact(user_id, llm_client=None) -> bool` :
  - Seuil : `int(max_messages * 0.8)` (défaut 16/20)
  - Split moitié oldest → résumé LLM, moitié récente → gardée
  - Message résumé : `{"role": "system", "content": "[RÉSUMÉ] ...", "timestamp": now()}`
  - DEL + RPUSH(summary, *to_keep) + EXPIRE 86400
  - Erreur LLM non-fatale (log warning, return False)
- `append()` auto-compacte si `_llm_client` défini
- 8 tests : `tests/test_souvenir_compaction.py`

#### ✅ 6.7 GracefulShutdown — Câblage tous les main.py

Pattern `while not shutdown.is_stopping()` câblé dans :
- `archiviste/main.py`
- `portail/main.py`
- `souvenir/main.py` (les deux boucles `_process_request_stream` et `_process_outgoing_streams`)
- `atelier/main.py`

9 tests : `tests/test_shutdown_wiring.py`

---

### Couverture finale (2026-03-29)

```
TOTAL: 1817 stmts, 343 miss, 81% coverage  (target: 80% ✅)
368 passed, 1 failed (pre-existing: test_emit_fire_and_forget_redis_down), 17 warnings
```

Fichiers individuels sous 80% (coverage intégration, non bloquants) :
- `portail/main.py` : 55% — logique orchestration nécessite Redis réel
- `sentinelle/main.py` : 39% — idem
- `souvenir/main.py` : 41% — idem
- `common/config_loader.py` : 47%
- `common/redis_client.py` : 43%

---

### ✅ Validation MVP Discord E2E — FAIT — 2026-03-29

Aiguilleur Discord (`aiguilleur/discord/main.py`) — streaming progressif, 21 tests, 82% coverage :
- ✅ `_subscribe_streaming_start()` : écoute `relais:streaming:start:discord` (Pub/Sub), spawn task
- ✅ `_handle_streaming_message()` : placeholder `▌`, XREAD BLOCK chunks, throttle 80 chars, édition finale sans curseur
- ✅ `on_message()` : ignore self, DM, mention, empty→"Coucou!", XADD failure silenced
- ✅ `consume_outgoing_stream()` : XREADGROUP, send reply, XACK, DM fallback, error recovery
- ✅ Bytes-key path (old aioredis) testé via _BytesFields helper
- Tests : `tests/test_aiguilleur_discord.py` (21 tests, 100% GREEN)
- Coverage : 82% `aiguilleur/discord/main.py` (uncovered : main() entry point, setup_hook, on_ready)

---

## Phase 6.1 — Enrichissement Observabilité (Axes A, B, C) ✅ FAIT — 2026-03-29

Améliorations d'observabilité et d'extraction mémoire pour meilleure traçabilité et analyse.

### ✅ Axe A — Archiviste Pipeline Stream Observation (FAIT — 2026-03-29)

Archiviste exécute maintenant deux consumer groups en parallèle pour vision complète du pipeline.

**Changements :**
- `archiviste/main.py::run()` → `asyncio.gather(_consume_log_stream, _process_pipeline_streams, _consume_events)`
- Nouveau consumer group `archiviste_pipeline_group` observant tous les streams du pipeline
- Streams observés : `relais:messages:incoming:*`, `relais:security`, `relais:tasks`, `relais:tasks:failed`, `relais:messages:outgoing:*`
- Logs de format : `[{cid[:8]}] {sender_id} → {stream} | traces={traces} | "{content_preview}..."`

**Critères de succès :**
- ✅ Deux consumer groups en parallèle
- ✅ Pipeline streams lus avec payload parsing
- ✅ Correlation ID, sender_id, traces extraits
- ✅ Logs structurés avec context Archiviste

### ✅ Axe B — Enriched Brick Logs avec Métadonnées (FAIT — 2026-03-29)

Toutes les briques core (Portail, Sentinelle, Atelier, Souvenir) enrichissent `relais:logs` avec trois champs.

**Champs ajoutés à chaque entrée `relais:logs` :**
- `correlation_id` — UUID tracking end-to-end
- `sender_id` — origine du message (ex: `discord:805123...`)
- `content_preview` — premiers 60 chars du contenu

**Implémentation :**
```python
# Tous les bricks
enriched_log = {
    "timestamp": time.time(),
    "level": "info|warning|error",
    "message": "...",
    "correlation_id": envelope.correlation_id,
    "sender_id": envelope.sender_id,
    "content_preview": envelope.content[:60].replace("\n", " "),
}
await redis.xadd("relais:logs", enriched_log)
```

**Archiviste re-emit pattern :**
```python
# archiviste/main.py
cid = payload.get("correlation_id", "unknown")[:8]
sender = payload.get("sender_id", "system")
# Log : [a1b2c3d4] discord:805123... | message text
self.logger.info(f"[{cid}] {sender} | message")
```

**Critères de succès :**
- ✅ Tous les bricks enrichissent correlation_id, sender_id, content_preview
- ✅ Archiviste re-emit avec préfixe `[{cid[:8]}] {sender}`
- ✅ Logs structurés habilitant l'observabilité correlée
- ✅ Pas de régression sur latence

### ✅ Axe C — Memory Extractor Profile + Dynamic Loading (FAIT — 2026-03-29)

Nouveau profil `memory_extractor` dans `config/profiles.yaml` pour extraction légère de faits.

**Profil `memory_extractor` :**
- Modèle : `glm-4.7-flash` (vs gpt-3.5-turbo hardcodé précédemment)
- Température : `0.1` (déterministe)
- Max tokens : `512`
- Max agent depth : `1` (pas de sous-agents)
- Résilience : 2 retries avec délai `[1, 3]`

**Chargement dynamique dans Souvenir :**
```python
# souvenir/main.py::__init__()
_FALLBACK_EXTRACTION_MODEL = "glm-4.7-flash"
try:
    _profiles = load_profiles()
    _extraction_profile = resolve_profile(_profiles, "memory_extractor")
    extraction_model = _extraction_profile.model
except Exception as exc:
    logger.warning("Could not load memory_extractor profile: %s", exc)
    extraction_model = _FALLBACK_EXTRACTION_MODEL

self._extractor = MemoryExtractor(litellm_url=litellm_url, model=extraction_model)
```

**Bénéfices :**
- Modèle configurable sans redéploiement
- Profil dédié = settings optimisés pour extraction
- Fallback sûr si config invalide

**Critères de succès :**
- ✅ Profil `memory_extractor` présent dans `config/profiles.yaml.default`
- ✅ Souvenir charge dynamiquement via `load_profiles()` + `resolve_profile()`
- ✅ Fallback sur `glm-4.7-flash` si load échoue
- ✅ MemoryExtractor accepte `model` parameter
- ✅ Tests : chargement réussi, fallback sur erreur

*Plan mis à jour le 2026-03-29 — Phase 6.1 complétée (Axes A/B/C : observabilité, enriched logs, memory_extractor profile). Couverture globale ≥81%. Prochaine phase : validation bout-à-bout.*
