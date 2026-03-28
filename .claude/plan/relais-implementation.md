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

## Phase 5 — Canaux supplémentaires L'Aiguilleur (priorité moyenne)

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

## Phase 2.2-bis — Migration Atelier : LiteLLM HTTP → SDK `anthropic` officiel (2026-03-28)

> **⚠️ Correction post-docs-lookup (2026-03-28) :**
> `claude-code-sdk` (alias `claude-agent-sdk`) est un wrapper subprocess autour du CLI Claude Code — inadapté à un pipeline Redis Streams (latence subprocess par message, exceptions opaques `ProcessError`, pas de retry HTTP). Remplacé par le SDK `anthropic` Python officiel avec `ANTHROPIC_BASE_URL` → LiteLLM proxy.

> **Décisions architecturales :**
> 1. **SDK** : `anthropic` Python officiel (`pip install anthropic`) remplace `httpx` + LiteLLM direct. `AsyncAnthropic(base_url=ANTHROPIC_BASE_URL, api_key=ANTHROPIC_API_KEY)` → LiteLLM proxy qui route vers n'importe quel backend (Mistral, Qwen, Claude, LM Studio).
> 2. **Routing modèle** : `ANTHROPIC_BASE_URL` → proxy LiteLLM existant. Les noms de modèles dans `profiles.yaml` deviennent des alias LiteLLM. Aucune migration de `profiles.yaml` nécessaire. LiteLLM doit exposer `/v1/messages` (format Anthropic natif).
> 3. **Contexte conversationnel** : Souvenir reste **propriétaire unique** de l'historique conversationnel. Redis List `relais:context:{session_id}` = cache rapide (TTL 24h) ; SQLite = source de vérité (fallback si Redis redémarre). Atelier demande l'historique via `relais:memory:request` (action `get`) avant chaque appel LLM — Souvenir répond via `relais:memory:response`. L'action `append` est supprimée : Souvenir alimente le contexte en observant `relais:messages:outgoing:{channel}` (il y a le `user_message` dans metadata + la réponse assistant dans le content).
> 4. **Prompts multi-couches** : SOUL.md + prompt rôle + prompt user + prompt canal + prompt policy — chaque couche optionnelle.
> 5. **Mémoire long-terme** : Souvenir observe `relais:messages:outgoing:{channel}` et déclenche un appel LLM fast (extracteur) pour persister des faits utilisateur cross-sessions. Aucun stream supplémentaire — même stream que l'archivage.

### ✅ Prérequis bloquant — LEVÉ (2026-03-28)

Vérification API surface `anthropic` SDK :
- **Entry point** : `await client.messages.create(model, max_tokens, system, messages)` → `Message`
- **Exceptions retry** : `APIConnectionError`, `APITimeoutError`, `InternalServerError` (502/503/529) depuis `anthropic`
- **Exceptions non-retriable** : `AuthenticationError` (401), `BadRequestError` (400)
- **Historique conversationnel** : stateless — passer `messages: list[MessageParam]` complet à chaque appel. Atelier demande l'historique à Souvenir via `relais:memory:request` (action `get`) avant chaque appel LLM. Souvenir gère Redis List (cache) + SQLite (fallback restart).
- **Compatibilité LiteLLM** : `AsyncAnthropic(base_url="http://localhost:4000", api_key="litellm-master-key")` — LiteLLM expose `/v1/messages` (format Anthropic natif). ✅ Confirmé supporté.
- **MCP** : non natif dans le SDK `anthropic`. Approche : convertir MCP tools en `ToolParam` definitions via `mcp` Python client.

**Pattern exécuteur :**
```python
import anthropic
from anthropic import APIConnectionError, APITimeoutError
from anthropic._exceptions import InternalServerError, AuthenticationError, BadRequestError

client = anthropic.AsyncAnthropic(
    base_url=os.environ["ANTHROPIC_BASE_URL"],
    api_key=os.environ["ANTHROPIC_API_KEY"],
)

RETRIABLE = (APIConnectionError, APITimeoutError, InternalServerError)

message = await client.messages.create(
    model=profile.model,
    max_tokens=profile.max_tokens,
    system=system_prompt,
    messages=conversation_history,   # list[MessageParam]
    tools=mcp_tool_definitions,      # optionnel
)
reply = message.content[0].text
```

### Phase A — Nouveaux modules support (sans breaking change)

Peuvent être développés et testés en parallèle :

| Fichier | Action | Responsabilité |
|---------|--------|----------------|
| `atelier/profile_loader.py` | CRÉER | Charge `profiles.yaml`, résout profil par nom, retourne `ProfileConfig` frozen dataclass |
| `atelier/soul_assembler.py` | CRÉER | Assemble `SOUL.md` + prompt rôle + prompt user + prompt canal + prompt policy → `system_prompt: str` |
| `atelier/mcp_loader.py` | CRÉER | Charge `mcp_servers.yaml`, filtre par profil, retourne liste SDK-compatible |
| `tests/test_profile_loader.py` | CRÉER | Unit tests profile_loader |
| `tests/test_soul_assembler.py` | CRÉER | Unit tests soul_assembler |
| `tests/test_mcp_loader.py` | CRÉER | Unit tests mcp_loader |

**`ProfileConfig` dataclass :**
```python
@dataclass(frozen=True)
class ProfileConfig:
    model: str
    temperature: float
    max_tokens: int
    resilience: ResilienceConfig  # retry_attempts, retry_delays, fallback_model

@dataclass(frozen=True)
class ResilienceConfig:
    retry_attempts: int
    retry_delays: list[int]
    fallback_model: str | None
```

**`soul_assembler.assemble_system_prompt(channel, sender_id=None, user_role=None, reply_policy=None, user_facts=None)` :**

Assembly en couches ordonnées, chaque couche optionnelle (warning si absente, silencieux si non applicable) :

| Ordre | Source | Chemin | Toujours présent |
|-------|--------|--------|-----------------|
| 1 | Personnalité de base | `soul/SOUL.md` | Oui (erreur si absent) |
| 2 | Prompt par rôle | `prompts/roles/{role}.md` | Non |
| 3 | Prompt par utilisateur | `prompts/users/{sender_id}.md` | Non |
| 4 | Prompt par canal | `prompts/channels/{channel}.md` | Non (warning) |
| 5 | Prompt policy | `prompts/policies/{reply_policy}.md` | Non |
| 6 | Mémoire long-terme | Injecté depuis SQLite Souvenir | Non |

Section mémoire long-terme injectée en fin de system_prompt :
```
## Mémoire utilisateur
{fact_1}
{fact_2}
...
```
Chargée via `souvenir.long_term_store.get_user_facts(sender_id)` avant assembly.

Concaténation : `\n\n---\n\n` entre chaque bloc non-vide.

Structure dossier `prompts/` à créer :
```
prompts/
├── roles/
│   ├── admin.md
│   └── user.md
├── users/               ← créé par l'utilisateur selon besoin
├── channels/
│   ├── discord.md
│   ├── telegram.md
│   └── whatsapp.md
└── policies/
    ├── in_meeting.md
    ├── out_of_hours.md
    └── vacation.md
```
Les fichiers `prompts/*.md` existants (racine) sont déplacés vers `prompts/channels/` et `prompts/policies/`.

**`mcp_loader.load_mcp_servers(profile_name=None)` :**
- Serveurs `global` où `enabled: true`
- Serveurs `contextual` où `enabled: true` ET profil dans `profiles`
- Retourne `[]` si config absente (dégradation gracieuse)

### Phase B — Remplacement executor (breaking change atelier/)

#### B.1 Réécriture `atelier/executor.py`

- SUPPRIMER : httpx.AsyncClient, POST `/chat/completions`, `RETRIABLE = (httpx.ConnectError, ...)`
- GARDER : `ExhaustedRetriesError` (même import, même comportement)
- NOUVELLE SIGNATURE :
```python
async def execute_with_resilience(
    envelope: Envelope,
    system_prompt: str,
    model: str,
    max_tokens: int,
    temperature: float,
    retry_delays: list[int],
    mcp_servers: list[dict] | None = None,
) -> str
```
- Appelle `client.messages.create()` via `anthropic.AsyncAnthropic` avec `base_url=ANTHROPIC_BASE_URL`
- Retry loop identique (backoff depuis `retry_delays` du profil)
- `RETRIABLE = (APIConnectionError, APITimeoutError, InternalServerError)`
- Extrait le texte : `message.content[0].text`

#### B.2 Mise à jour `atelier/main.py`

SUPPRIMER :
- `_get_memory_context()` (lignes 43-100)
- `_append_assistant_memory()` (lignes 102-119)
- `httpx` import et `httpx.AsyncClient` context manager
- `self.litellm_url`, `self.litellm_key`, `self.litellm_model`

AJOUTER dans `__init__` :
- `self.profiles = profile_loader.load_profiles()`
- `self.soul_cache: dict[str, str] = {}`

METTRE À JOUR `_handle_message()` :
1. Parser l'envelope
2. Résoudre profil : `envelope.metadata.get("llm_profile", "default")`
3. Demander historique à Souvenir : publier `{action: "get", session_id, correlation_id}` sur `relais:memory:request`, attendre réponse sur `relais:memory:response` (timeout 3s, défaut `[]` si timeout)
4. Assembler system_prompt via `soul_assembler` (avec `sender_id`, `user_role`, `reply_policy`, `user_facts`)
5. Charger MCP servers via `mcp_loader`
6. Appeler `executor.execute_with_resilience(messages=history + [user_msg], ...)`
7. Construire response envelope via `Envelope.create_response_to()`
8. Injecter message utilisateur original dans `response_env.metadata["user_message"] = envelope.content` ← **pour Souvenir** (alimente Redis List + SQLite + extracteur)
9. Publier response envelope sur `relais:messages:outgoing:{channel}`
10. Souvenir observe et met à jour l'historique (plus d'appel `append` explicite)

### Phase C — Refonte Souvenir

#### C.1 Mise à jour `souvenir/main.py`

Souvenir gère maintenant **deux streams** :

**Stream 1 : `relais:memory:request`** (consumer group existant)
- SUPPRIMER : action `append` (remplacée par observation outgoing)
- GARDER : action `get` → lire Redis List `relais:context:{session_id}` (cache) ; si vide → lire SQLite (fallback restart) → publier sur `relais:memory:response`

**Stream 2 : `relais:messages:outgoing:{channel}`** (nouveau consumer group)
- Sur chaque message reçu, déclencher `_handle_outgoing(envelope)` :
  1. **Mise à jour contexte** : RPUSH `[user_message, assistant_reply]` dans Redis List `relais:context:{session_id}` + LTRIM -20 -1 + EXPIRE 24h
  2. **Archivage long-terme** : INSERT SQLite (comportement existant)
  3. **Extraction mémoire** : appel LLM fast → faits utilisateur → SQLite (nouveau)
- ⚠️ Redis Streams ne supporte pas les wildcard consumer groups — Souvenir s'abonne aux canaux connus explicitement (liste depuis `config.yaml`)

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

**Nouveaux champs SQLite `user_facts` table :**
```sql
CREATE TABLE user_facts (
    id          TEXT PRIMARY KEY,
    sender_id   TEXT NOT NULL,
    fact        TEXT NOT NULL,
    category    TEXT,          -- "preference", "context", "identity", ...
    confidence  REAL,
    source_corr TEXT,          -- correlation_id de l'échange source
    created_at  REAL,
    updated_at  REAL
);
```
Migration Alembic requise.

**`souvenir/memory_extractor.py`** — CRÉER :
- `async def extract(envelope: Envelope, http_client: httpx.AsyncClient) -> list[UserFact]`
- Appelle LiteLLM proxy directement (httpx, profil `fast`)
- Parse JSON, filtre confidence, retourne liste
- Fire-and-forget acceptable (non-bloquant pour la conversation principale)

#### C.2 Mise à jour `souvenir/long_term_store.py`
- Ajouter `upsert_facts(sender_id, facts: list[UserFact]) -> None`
- Ajouter `get_user_facts(sender_id, limit=20) -> list[str]` (retourne texte brut pour injection system_prompt)
- Upsert par (sender_id + fact hash) pour éviter les doublons

#### C.3 Mise à jour `souvenir/context_store.py`
- GARDER (ne pas supprimer)
- Ajouter `get_recent(session_id, limit=20) -> list[MessageParam]` : lit Redis List ; si vide → lit SQLite via `long_term_store.get_recent_messages(session_id, limit)`
- Ajouter `append_turn(session_id, user_content, assistant_content)` : RPUSH + LTRIM + EXPIRE

### Phase D — Configuration & Environnement

| Fichier | Action |
|---------|--------|
| `pyproject.toml` | Ajouter `anthropic >= 0.40`. Vérifier si `httpx` encore utilisé ailleurs avant suppression (Souvenir extracteur l'utilise encore). |
| `.env.example` | Ajouter `ANTHROPIC_BASE_URL=http://localhost:4000/v1` et `ANTHROPIC_API_KEY=sk-changeme` |
| `supervisord.conf` | Ajouter `ANTHROPIC_BASE_URL` et `ANTHROPIC_API_KEY` dans `[program:atelier]` environment |
| `config/redis.conf` | ACL atelier : garder `relais:memory:request` (write) et `relais:memory:response` (read). Supprimer accès direct `relais:context:*` (Souvenir seul y écrit). |

### Phase E — Tests

| Fichier | Action |
|---------|--------|
| `tests/test_atelier.py` | RÉÉCRIRE : mock SDK `anthropic` au lieu de httpx. Conserver tests XACK (critiques). Supprimer tests `_get_memory_context`/`_append_assistant_memory`. Ajouter : tests résolution profil, assembly system_prompt multi-couches, MCP passthrough, injection `user_message` dans metadata, mock request `get` vers Souvenir (timeout + réponse normale). |
| `tests/test_soul_assembler.py` | CRÉER : assembly avec 0-6 couches, couche absente ignorée, mémoire utilisateur injectée en fin, ordre des blocs correct. |
| `tests/test_souvenir.py` | Mettre à jour `ContextStore` : tester `get_recent` (cache hit Redis, cache miss → SQLite fallback), `append_turn` (RPUSH + LTRIM + EXPIRE). Conserver tous les tests `LongTermStore`. Ajouter tests `memory_extractor` (mock LLM call, parse JSON, filtre confidence). Ajouter tests `get_user_facts` / `upsert_facts`. Ajouter test flux `get` complet (request/response correlation_id). |

### Phase F — Documentation

| Fichier | Changements |
|---------|-------------|
| `CLAUDE.md` | Atelier : LiteLLM httpx → SDK `anthropic` officiel (`AsyncAnthropic`) + request `get` vers Souvenir avant LLM + injection `user_message` en metadata sortante. Souvenir : dual-stream (memory:request `get` + outgoing observer), context_store Redis+SQLite fallback, extracteur. Env vars : `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`. Structure `prompts/` : sous-dossiers channels/roles/users/policies. |
| `docs/ARCHITECTURE.md` | Atelier : remplacer description LiteLLM, documenter assembly system_prompt multi-couches + flux get historique. Souvenir : documenter dual-stream, context_store Redis+SQLite fallback, extracteur + table `user_facts`. Flux Redis : garder `relais:memory:request/response` (action `get` uniquement). |
| `plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md` | Mettre à jour sections Atelier, Souvenir, table SQLite, dépendances. |
| `README.md` | Liste dépendances, variables d'environnement, structure `prompts/` mise à jour. |
| `prompts/` | Réorganiser : déplacer fichiers `*_default.md` → `channels/`, `in_meeting.md`/`vacation.md`/`out_of_hours.md` → `policies/`. Créer dossiers `roles/`, `users/`. |

### Ordre d'exécution

```
Phase D.1 pyproject.toml          ← ajouter anthropic >= 0.40
Phase F.prompts réorganisation    ← déplacer fichiers prompts/ (sans casser l'existant)

Phase A (parallèle) :
  profile_loader + tests
  soul_assembler + tests           ← intègre couches rôle/user/canal/policy + injection mémoire
  mcp_loader + tests

[BLOCKER levé] API `anthropic` SDK vérifiée le 2026-03-28

Phase B (séquentiel) :
  B.1 executor.py
  B.2 main.py                     ← inclut injection user_message dans metadata

Phase C (séquentiel) :
  C.1 souvenir/main.py + memory_extractor.py + migration Alembic user_facts
  C.2 souvenir/long_term_store.py (upsert_facts, get_user_facts)
  C.3 supprimer context_store.py

Phase D (reste, parallèle) :
  .env.example
  supervisord.conf
  redis.conf ACL

Phase E : tests (après code)
Phase F : documentation (en dernier)
```

### Risques

| Risque | Sévérité | Mitigation |
|--------|----------|------------|
| ~~API claude-agent-sdk non vérifiée~~ | ~~HAUT~~ | ✅ **Levé** — SDK `anthropic` officiel utilisé, API vérifiée |
| SDK `anthropic` stateless → historique conversationnel | ✅ **Résolu** | Souvenir propriétaire unique. Redis List = cache (TTL 24h), SQLite = fallback si Redis redémarre. Atelier demande via `relais:memory:request` (get). |
| LiteLLM `/v1/messages` format Anthropic — à tester | **MOYEN** | LiteLLM supporte `/v1/messages` (Anthropic-compatible endpoint). Tester avec `curl -X POST http://localhost:4000/v1/messages` avant Phase B. |
| Extracteur mémoire pollue SQLite (faux positifs) | Moyen | Seuil confidence 0.7, prompt extraction strict, revue manuelle possible via Vigile |
| Wildcard streams Souvenir | Moyen | Abonnement explicite aux canaux connus (liste dans config.yaml) |
| prompts/ réorganisation casse portail/prompt_loader.py | Moyen | Mettre à jour prompt_loader en même temps que la réorganisation |
| httpx supprimé casse d'autres briques | Moyen | grep `import httpx` avant suppression (Souvenir extracteur en a encore besoin) |
| Sentinelle n'injecte pas encore `llm_profile`/`user_role` | Faible | Défaut `"default"` → dégradation gracieuse |

### Critères de succès

- [ ] `anthropic >= 0.40` installé et importable
- [ ] `atelier/executor.py` utilise `anthropic.AsyncAnthropic`, même contrat résilience (ExhaustedRetriesError, DLQ, PEL)
- [ ] `atelier/main.py` résout profil LLM par user depuis metadata envelope
- [ ] Atelier demande historique via `relais:memory:request` (get) avant chaque appel LLM (timeout 3s, fallback `[]`)
- [ ] System prompt inclut toutes les couches : SOUL + rôle + user + canal + policy + faits mémoire
- [ ] `user_message` injecté dans `metadata` de l'envelope réponse (pour Souvenir)
- [ ] MCP servers chargés depuis config et passés au SDK
- [ ] Souvenir répond aux `get` depuis Redis List ; SQLite comme fallback si cache vide
- [ ] Souvenir alimente Redis List via observation `relais:messages:outgoing:{channel}` (RPUSH user+assistant, LTRIM 20, EXPIRE 24h)
- [ ] Souvenir archive les envelopes sortantes dans SQLite long-terme
- [ ] Souvenir déclenche extracteur mémoire sur chaque message sortant
- [ ] Table `user_facts` créée via migration Alembic
- [ ] Faits extraits disponibles au prochain `soul_assembler` (injection mémoire cross-session)
- [ ] Tests XACK (success, DLQ, generic error, retriable) toujours verts
- [ ] Nouveaux unit tests profile_loader, soul_assembler (multi-couches), mcp_loader passent
- [ ] Tests extracteur mémoire (mock LLM, parse, filtre confidence)
- [ ] Smoke test complet : Discord → réponse → archivage SQLite + extraction faits
- [ ] Couverture ≥ 80% sur atelier/ et souvenir/

---

*Plan mis à jour le 2026-03-28 — Migration Atelier SDK `anthropic` officiel (AsyncAnthropic) + prompts multi-couches + extracteur mémoire Souvenir. claude-code-sdk écarté (subprocess, inadapté pipeline Redis). Souvenir reste propriétaire unique du contexte conversationnel (Redis List cache + SQLite fallback restart). Atelier demande l'historique via relais:memory:request (get).*
