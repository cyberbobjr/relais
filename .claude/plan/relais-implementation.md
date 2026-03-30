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

## Phase 5a — Corrections MCP & Profils (2026-03-30) ✅ DONE (Points 1–4)

### 5a.1 ✅ Format YAML `mcp_servers.yaml` — clé racine `mcp_servers:` DONE

**Problème :** les fichiers `mcp_servers.yaml.default` et `.relais/config/mcp_servers.yaml` définissaient `global:` et `contextual:` à la racine, sans clé `mcp_servers:`. Seul `load_mcp_servers()` était cohérent ; la documentation et les commentaires YAML étaient incohérents.

**Correction :** format canonique normalisé — clé racine `mcp_servers:` obligatoire, `global:` et `contextual:` imbriqués dessous. `timeout:` et `max_tools:` supprimés du YAML (déplacés en Point 4). Les fixtures de tests mises à jour en conséquence.

**Fichiers modifiés :** `config/mcp_servers.yaml.default`, `.relais/config/mcp_servers.yaml`, `tests/test_mcp_loader.py`

### 5a.2 ✅ Support transport SSE dans `McpServerConfig` et `SDKExecutor` DONE

**Problème :** `McpServerConfig` n'avait ni champ `type` ni champ `url`. La documentation YAML mentionnait les serveurs SSE mais le code ignorait ce transport.

**Correction :**
- `McpServerConfig` : ajout de `type: str = "stdio"` et `url: str | None = None`
- `load_for_sdk()` : retourne `{type, url, env?}` pour SSE vs `{type, command, args, env?}` pour stdio
- `SDKExecutor._start_mcp_servers()` : branche sur `cfg.get("type", "stdio")` — stdio → `stdio_client`, sse → `sse_client` (guard `_SSE_AVAILABLE`), inconnu → warning + skip
- Fixtures SSE ajoutées dans `tests/test_mcp_loader.py` (tests 17, 18, 19)

**Fichiers modifiés :** `atelier/mcp_loader.py`, `atelier/sdk_executor.py`, `tests/test_mcp_loader.py`

### 5a.4 ✅ Extraction McpSessionManager — refactoring lisibilité DONE (2026-03-30)

**Problème :** `SDKExecutor` mêlait logique LLM et infrastructure MCP (`_start_mcp_servers()`, `_call_mcp_tool()`). La méthode `_get_anthropic_tools()` reconstruisait les schémas d'outils internes à chaque appel alors qu'ils étaient déjà au bon format (produit par `make_skills_tools()`). Trop d'indirection pour suivre d'où viennent les outils.

**Correction :**
- `McpSessionManager` (`atelier/mcp_session_manager.py`) : nouveau module isolant tout le cycle de vie MCP (démarrage stdio/SSE, sessions internes, dispatch avec `asyncio.wait_for`, timeout configurable)
- `SDKExecutor` : suppression de `_start_mcp_servers()` et `_call_mcp_tool()` ; les schémas internes sont pré-calculés une fois dans `__init__` (`_internal_tool_schemas`) ; `_get_anthropic_tools()` réduite à une concaténation + cap
- Tests MCP migrés vers `tests/test_mcp_session_manager.py` (6 tests)

**Fichiers modifiés :** `atelier/sdk_executor.py`, `atelier/mcp_session_manager.py` (nouveau), `tests/test_sdk_executor.py`, `tests/test_mcp_session_manager.py` (nouveau)

### 5a.3 ✅ Suppression section `subagents:` DONE

**Problème :** `profile_loader.py` contenait encore `max_agent_depth` (vestige de l'architecture sous-agents abandonnée). La section `subagents:` dans le document fondateur était obsolète.

**Correction :** suppression de `max_agent_depth` dans `ProfileConfig` et `config/profiles.yaml.default`. Mise à jour du document fondateur et de CLAUDE.md.

**Fichiers modifiés :** `atelier/profile_loader.py`, `config/profiles.yaml.default`, `plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md`, `CLAUDE.md`

---

### 5a.4 ✅ `mcp_timeout` et `mcp_max_tools` dans `profiles.yaml` — DONE (2026-03-30)

**Objectif :** déplacer `timeout` et `max_tools` de `mcp_servers.yaml` vers `profiles.yaml`, per-profil. Chaque profil LLM définit ses propres contraintes MCP selon son contexte d'usage.

**Fichiers à modifier :**

| Fichier | Changement |
|---------|-----------|
| `atelier/profile_loader.py` | Ajouter `mcp_timeout: int = 10` et `mcp_max_tools: int = 20` à `ProfileConfig` |
| `atelier/sdk_executor.py` | `asyncio.wait_for(_call_mcp_tool(...), timeout=profile.mcp_timeout)` + tronquer la liste d'outils à `profile.mcp_max_tools` |
| `config/profiles.yaml.default` | Ajouter `mcp_timeout` et `mcp_max_tools` à chaque profil (valeurs différenciées) |
| `tests/test_sdk_executor.py` | Tests pour timeout et max_tools |
| `CLAUDE.md` | Documenter les nouveaux champs dans la section ProfileConfig |

**Valeurs cibles par profil :**
```yaml
default:   mcp_timeout: 10   mcp_max_tools: 20
fast:      mcp_timeout: 5    mcp_max_tools: 10
precise:   mcp_timeout: 30   mcp_max_tools: 30
coder:     mcp_timeout: 20   mcp_max_tools: 40
memory_extractor: mcp_timeout: 5  mcp_max_tools: 0
```

---

## Phase 5c — Déduplication messages Discord streaming (2026-03-30) ✅ DONE

### 5c.1 ✅ Bug : doublon de message Discord après streaming — RÉSOLU

**Symptôme :** après un streaming complet (`is_final=1`), l'enveloppe finale publiée par Atelier sur `relais:messages:outgoing:discord` était envoyée comme **nouveau message** par `consume_outgoing_stream`, créant un doublon du message déjà streamé.

**Cause racine :** deux tâches asyncio dans `RelaisDiscordClient` s'exécutent en parallèle :
- `_subscribe_streaming_start` → `_handle_streaming_message` : édite le placeholder `▌` progressivement
- `consume_outgoing_stream` : envoie l'enveloppe finale comme nouveau message via `channel.send()`

**Solution implémentée (Option C — metadata + Redis String) :**

1. **`aiguilleur/discord/main.py` — `_handle_streaming_message`** : après `channel.send("▌")`, stocke l'ID du message Discord dans Redis :
   ```python
   await self._redis.setex(f"relais:streamed_msg:{envelope.correlation_id}", 300, str(msg.id))
   ```

2. **`aiguilleur/discord/main.py` — `consume_outgoing_stream`** : si `metadata["streamed"]` est `True`, édite le message existant au lieu d'en envoyer un nouveau :
   ```python
   if envelope.metadata.get("streamed"):
       redis_key = f"relais:streamed_msg:{envelope.correlation_id}"
       discord_msg_id = await self.redis_conn.get(redis_key)
       if discord_msg_id:
           partial = channel.get_partial_message(int(discord_msg_id))
           await partial.edit(content=envelope.content)
           await self.redis_conn.delete(redis_key)
       else:
           await channel.send(envelope.content)  # fallback TTL expiré
   else:
       await channel.send(envelope.content)
   ```

3. **`atelier/main.py`** : après `stream_pub.finalize()`, positionne le flag dans l'enveloppe de réponse :
   ```python
   if stream_pub is not None:
       response_env.metadata["streamed"] = True
   ```

**Tests ajoutés (TDD RED → GREEN) :**
- `tests/test_aiguilleur_discord.py` : 4 tests (setex au placeholder, edit si clé présente, send si non-streamé, fallback send si TTL expiré)
- `tests/test_atelier.py` : 2 tests (flag présent pour canal streaming, absent pour canal non-streaming)

**Clé Redis introduite :** `relais:streamed_msg:{correlation_id}` — String, TTL 300s, valeur = Discord message ID (int as string).

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

## Phase 2.2-bis — Migration Atelier : `anthropic` + `mcp` Python SDK + Streaming progressif ✅ FAIT — 2026-03-30

> **Décision initiale (2026-03-28) :** `claude-agent-sdk` avait été choisi pour les subagents natifs et le MCP natif.
> **Décision finale (2026-03-30) :** Migration vers le SDK Python natif `anthropic` + `mcp`, suite à la suppression de `claude-agent-sdk` (erreurs silencieuses, dépendance CLI Node.js non fiable). Le bug #677 et le workaround `shutil.which("claude")` sont obsolètes.

### Décisions architecturales

1. **SDK** : `anthropic.AsyncAnthropic(base_url=..., api_key=...)` — appel direct LiteLLM proxy. Aucune dépendance CLI.
2. **Routing modèle** : `ANTHROPIC_BASE_URL=http://localhost:4000` → LiteLLM proxy. `ANTHROPIC_API_KEY=litellm-master-key`. Les alias modèles dans `profiles.yaml` sont des alias LiteLLM. Aucune migration de config nécessaire.
3. **MCP stdio** : `mcp` Python SDK (`mcp.client.stdio.stdio_client`) — serveurs stdio gérés via `contextlib.AsyncExitStack`. `mcp_loader.load_for_sdk()` retourne le format dict attendu.
4. **Outils internes** : `InternalTool` frozen dataclass (name/description/input_schema/handler). `make_skills_tools()` expose `list_skills` + `read_skill`. Remplace les subagents `AgentDefinition`.
5. **Streaming progressif** : `StreamPublisher` → `relais:messages:streaming:{channel}:{corr_id}` → Aiguilleur édite le message Discord/Telegram en temps réel (throttle 80 chars, XREAD BLOCK 150ms).
6. **Contexte conversationnel** : Souvenir reste **propriétaire unique** de l'historique. Redis List `relais:context:{session_id}` = cache rapide (TTL 24h) ; SQLite = source de vérité (fallback si Redis redémarre). Atelier demande via `relais:memory:request` (action `get`) avant chaque appel SDK.

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

### ✅ Phase B — Remplacement executor (FAIT — 2026-03-30)

#### B.1 ✅ `atelier/sdk_executor.py` créé + `atelier/mcp_loader.py` modifié

**`atelier/sdk_executor.py`** — CRÉÉ avec `anthropic.AsyncAnthropic` + boucle agentique explicite :
```python
import anthropic, os

class SDKExecutionError(Exception): pass

class SDKExecutor:
    def __init__(self, profile, soul_prompt, mcp_servers, tools):
        self._client = anthropic.AsyncAnthropic(
            base_url=os.environ.get("ANTHROPIC_BASE_URL", "http://localhost:4000"),
            api_key=os.environ.get("ANTHROPIC_API_KEY",
                     os.environ.get("ANTHROPIC_AUTH_TOKEN", "")),
        )
        # ...

    async def execute(self, envelope, context, stream_callback=None) -> str:
        messages = self._build_messages(envelope, context)
        async with AsyncExitStack() as stack:
            # Lance les serveurs MCP stdio via mcp.client.stdio.stdio_client
            mcp_tools = await self._init_mcp_servers(stack)
            all_tools = self._get_anthropic_tools() + mcp_tools
            return await self._run_agentic_loop(messages, all_tools, stream_callback)

    async def _run_agentic_loop(self, messages, tools, stream_callback) -> str:
        full_reply = ""
        while True:
            async with self._client.messages.stream(
                model=self._profile.model,
                max_tokens=self._profile.max_tokens,
                system=self._soul_prompt,
                messages=messages,
                tools=tools,
            ) as stream:
                async for chunk in stream.text_stream:
                    full_reply += chunk
                    if stream_callback:
                        await stream_callback(chunk)
                final = await stream.get_final_message()
            if final.stop_reason != "tool_use":
                break
            # Traitement tool_use → tool_result → rebouclage
            ...
        return full_reply
```

**`atelier/internal_tool.py`** — `InternalTool` frozen dataclass :
```python
@dataclass(frozen=True)
class InternalTool:
    name: str
    description: str
    input_schema: dict
    handler: Callable
```

**`atelier/skills_tools.py`** — `make_skills_tools(skills_dir)` → `[list_skills, read_skill]`.

**`atelier/mcp_loader.py`** — `load_for_sdk(profile_name)` :
```python
def load_for_sdk(profile_name: str = "default") -> dict:
    """Retourne {name: {command, args, env}} pour mcp.client.stdio.stdio_client."""
```
Format stdio : `{"command": ..., "args": [...], "env": {...}}`.

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
| `tests/test_sdk_executor.py` | ✅ MIS À JOUR (2026-03-30) : mock `anthropic.AsyncAnthropic`, test reply assemblé, test `stream_callback` appelé, test `SDKExecutionError` sur `APIStatusError`/`ConnectError`, test boucle tool-use, test `_build_messages`, `_get_anthropic_tools`, `_call_tool`. Tests MCP migrés vers `test_mcp_session_manager.py`. |
| `tests/test_mcp_session_manager.py` | ✅ CRÉÉ (2026-03-30) : `call_tool` server not found, timeout pass-through, TimeoutError → string, exception → string, `start_all` MCP unavailable, `start_all` no servers |
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

## Phase 5 — Outils internes + Serveurs MCP stdio ✅ FAIT — 2026-03-30

> **Révision (2026-03-30) :** Les subagents `AgentDefinition` / `load_subagents_for_sdk()` (dépendants de `claude-agent-sdk`) sont remplacés par `InternalTool` (skills locaux) + serveurs MCP stdio via le SDK `mcp` Python.

### ✅ Implémentation (FAIT — 2026-03-30)

| Composant | État | Notes |
|-----------|------|-------|
| `atelier/internal_tool.py::InternalTool` | ✅ CRÉÉ | `@dataclass(frozen=True)` name/description/input_schema/handler |
| `atelier/skills_tools.py::make_skills_tools()` | ✅ CRÉÉ | Retourne `[list_skills, read_skill]` InternalTools |
| `atelier/sdk_executor.py::SDKExecutor` | ✅ MIS À JOUR | Accepte `tools: list[InternalTool]`, schémas pré-calculés dans `__init__`, délègue MCP à `McpSessionManager` |
| `atelier/mcp_session_manager.py::McpSessionManager` | ✅ CRÉÉ (2026-03-30) | Cycle de vie MCP isolé : `start_all(stack)` + `call_tool()` avec timeout |
| `atelier/mcp_loader.py::load_for_sdk()` | ✅ MIS À JOUR | Retourne dict `{name: {command, args, env}}` pour `stdio_client` |
| `atelier/main.py` | ✅ MIS À JOUR | Appelle `make_skills_tools()`, passe `tools=` à SDKExecutor |
| `atelier/mcp_loader.py::SubagentConfig` | 🗑️ SUPPRIMÉ | Code mort supprimé avec les tests associés (`test_mcp_loader_subagents.py`) |
| `atelier/mcp_loader.py::load_subagents_for_sdk()` | 🗑️ SUPPRIMÉ | Idem |

### Flux d'exécution

```
atelier/main.py:
  → make_skills_tools(skills_dir)       # InternalTools skills/
  → load_for_sdk()                      # Serveurs MCP stdio
  → SDKExecutor(tools=tools, mcp_servers=mcp_servers)
  → McpSessionManager.start_all(stack)  # stdio/SSE par serveur MCP
  → _run_agentic_loop(mcp_manager) → messages.stream()
  → stop_reason == "tool_use" → _call_tool() → InternalTool | McpSessionManager.call_tool()
  → reboucle jusqu'à stop_reason == "end_turn"
  → relais:messages:outgoing:{channel}
```

### Critères de succès

- ✅ InternalTool frozen dataclass (immutable)
- ✅ make_skills_tools() retourne list_skills + read_skill
- ✅ SDKExecutor._call_tool() dispatche InternalTool et outils MCP (via McpSessionManager)
- ✅ Boucle agentique : tool_use → tool_result → rebouclage → end_turn
- ✅ MCP servers initialisés via McpSessionManager + AsyncExitStack (lifecycle propre)
- ✅ McpSessionManager isolé et testé indépendamment (`test_mcp_session_manager.py`)
- ✅ Aucune dépendance CLI Node.js (`claude`) requise
- ✅ anthropic >= 0.40.0, mcp >= 1.0.0

### Dépendances

- ✅ anthropic >= 0.40.0
- ✅ mcp >= 1.0.0
- ✅ Pydantic >= 2.9

---

| Risque | Sévérité | Mitigation |
|--------|----------|------------|
| Rate limit Discord (streaming) | MOYEN | Throttle 80 chars entre edits. En cas d'erreur 429 → ignorer l'édition intermédiaire |
| Extracteur mémoire pollue SQLite (faux positifs) | MOYEN | Seuil confidence 0.7, prompt strict, revue manuelle possible via Vigile |
| Wildcard streams Souvenir | MOYEN | Abonnement explicite aux canaux connus (liste dans `config.yaml`) |
| `prompts/` réorganisation casse `portail/prompt_loader.py` | MOYEN | Mettre à jour `prompt_loader` en même temps |
| Sentinelle n'injecte pas encore `llm_profile`/`user_role` | FAIBLE | Défaut `"default"` → dégradation gracieuse |

---

### Critères de succès

- [x] `anthropic.AsyncAnthropic` importable — `anthropic >= 0.40.0`
- [x] `atelier/sdk_executor.py` : `SDKExecutor.execute()` retourne texte, appelle `stream_callback`, lève `SDKExecutionError` sur `APIStatusError`/`ConnectError`
- [x] `atelier/stream_publisher.py` : `push_chunk()` XADD avec `seq` incrémenté, `finalize()` publie `is_final=1`
- [x] `atelier/mcp_loader.py` : `load_for_sdk()` retourne dict `{name: {command, args, env}}`
- [x] `atelier/profile_loader.py` : `ProfileConfig` a le champ `max_turns`
- [x] `atelier/main.py` : signal Pub/Sub `relais:streaming:start:{channel}` envoyé avant l'exécution
- [x] `atelier/main.py` : XACK conditionnel (success + DLQ → ACK ; exception générique → PEL)
- [x] `atelier/main.py` : `user_message` injecté dans `metadata` de l'envelope réponse
- [ ] Souvenir répond aux `get` depuis Redis List ; SQLite comme fallback si cache vide
- [ ] Souvenir alimente Redis List via observation `relais:messages:outgoing:{channel}`
- [ ] Souvenir déclenche extracteur mémoire sur chaque message sortant
- [ ] Table `user_facts` créée via migration Alembic
- [ ] Aiguilleur Discord : placeholder message créé + éditions progressives + édition finale sans curseur
- [x] Tests XACK (success, DLQ, generic error, retriable) toujours verts
- [x] Couverture ≥ 80% sur `atelier/` — 31 tests passent
- [ ] Smoke test complet : Discord → streaming visible → réponse finale → archivage SQLite

---

*Plan mis à jour le 2026-03-30 — Migration Atelier vers `anthropic.AsyncAnthropic` + `mcp` Python SDK (suppression `claude-agent-sdk`, boucle agentique explicite, `InternalTool` + serveurs MCP stdio). Souvenir reste propriétaire unique du contexte. Streaming Discord via `StreamPublisher` + `relais:messages:streaming:*`.*

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

---

## Phase 6.2 — Architecture AIGUILLEUR configurable ✅ FAIT — 2026-03-30

Unification et simplification de l'architecture AIGUILLEUR : un seul processus `aiguilleur/main.py` gère tous les adaptateurs de canaux via configuration centralisée `channels.yaml`, avec support des adaptateurs natifs (Python thread + asyncio) et externes (subprocess).

### ✅ Implémentation (FAIT — 2026-03-30)

| Composant | État | Notes |
|-----------|------|-------|
| `aiguilleur/channel_config.py::ChannelConfig` | ✅ CRÉÉ | Frozen dataclass : enabled, streaming, type, class_path, max_restarts |
| `aiguilleur/channel_config.py::load_channels_config()` | ✅ CRÉÉ | Cascade résolution : `~/.relais/config/` > `/opt/relais/config/` > `./config/` |
| `aiguilleur/core/base.py::BaseAiguilleur` | ✅ CRÉÉ | ABC : start/stop/is_alive/restart/health |
| `aiguilleur/core/native.py::NativeAiguilleur` | ✅ CRÉÉ | Thread OS + asyncio.run par adaptateur Python |
| `aiguilleur/core/external.py::ExternalAiguilleur` | ✅ CRÉÉ | subprocess.Popen pour adaptateurs non-Python |
| `aiguilleur/core/manager.py::AiguilleurManager` | ✅ CRÉÉ | Supervisor de lifecycle : dépoussiéreurs, redémarrages exponentiels |
| `aiguilleur/channels/discord/adapter.py::DiscordAiguilleur` | ✅ CRÉÉ | Migration de `aiguilleur/discord/main.py` → hérite `NativeAiguilleur` |
| `aiguilleur/main.py` | ✅ CRÉÉ | Entry point : charge `channels.yaml`, instancie `AiguilleurManager`, lance la boucle |
| `config/channels.yaml.default` | ✅ CRÉÉ | Définition de tous les canaux (enabled/disabled, streaming, type, class_path, max_restarts) |
| `aiguilleur/discord/main.py` | 🗑️ SUPPRIMÉ | Aucun stub de compatibilité (refonte complète) |
| `atelier/main.py::STREAMING_CAPABLE_CHANNELS` | ✅ MIS À JOUR | Chargé dynamiquement via `load_channels_config()` au lieu de liste en dur |
| Tests | ✅ CRÉÉS | `tests/test_channel_config.py`, `tests/test_aiguilleur_manager.py`, `tests/test_aiguilleur_discord.py` mis à jour |

### Conception clés

**Modèle de cycle de vie :**
```python
AiguilleurManager
├── load_channels_config()
├── _instantiate_adapters()
│   ├── NativeAiguilleur(channel_config) — thread OS, asyncio.run
│   └── ExternalAiguilleur(channel_config) — subprocess.Popen
└── run()
    ├── start()
    ├── monitor() — restart exponential backoff min(2^count, 30)s, max_restarts=5
    └── stop()
```

**Adaptateur natif (exemple Discord) :**
```python
class DiscordAiguilleur(NativeAiguilleur):
    async def run(self):
        client = discord.Client(...)
        await client.start(TOKEN)
```

**Adaptateur externe (exemple WhatsApp) :**
```python
ExternalAiguilleur(
    ChannelConfig(
        name="whatsapp",
        type="external",
        command="node",
        args=["aiguilleur/whatsapp/index.js"],
        ...
    )
)
```

**Restart automatique :**
- Délai = `min(2^restart_count, 30)` secondes (exponential backoff, capped 30s)
- Max redémarrages = `max_restarts` (défaut 5)
- Après exhaustion : **abandon silencieux** (log error, pas de crash manager)

**Découverte automatique des adaptateurs natifs :**
- Si `type: native` et pas de `class_path` → charge convention `aiguilleur.channels.{channel}.adapter.{Channel}Aiguilleur`
- Si `class_path` fourni → utilise le chemin custom
- Si découverte échoue → logs error, passe au channel suivant (graceful degradation)

### Fichiers supprimés

- `aiguilleur/discord/main.py` — entièrement remplacé par l'architecture configurable (aucune migration requise, le nouveau code gère tout)

### Critères de succès

- ✅ Un seul processus `aiguilleur/main.py` gère tous les adaptateurs
- ✅ Configuration centralisée `channels.yaml` (enabled/disabled, streaming, type, max_restarts)
- ✅ Support natif (thread OS + asyncio) et externe (subprocess)
- ✅ Restart automatique avec exponential backoff
- ✅ Adaptateurs découverts par convention (`aiguilleur.channels.{name}.adapter`)
- ✅ `atelier/main.py` : `STREAMING_CAPABLE_CHANNELS` chargé dynamiquement
- ✅ Tests : config loading, manager lifecycle, adapter instantiation, restart logic
- ✅ Couverture ≥ 80% sur `aiguilleur/`

### Changements observables

**Avant :**
```bash
supervisord.conf → [program:aiguilleur-discord], [program:aiguilleur-telegram], ...
                → Chaque canal = nouveau processus OS + fichier main.py dédié
```

**Après :**
```bash
supervisord.conf → [program:aiguilleur]
                → Un processus unique, tous les canaux gérés par AiguilleurManager
                → channels.yaml contrôle enabled/disabled sans redémarrage process
```

*Plan mis à jour le 2026-03-30 — Phase 6.2 complétée (Architecture AIGUILLEUR configurable : ChannelConfig, BaseAiguilleur ABC, NativeAiguilleur thread+asyncio, ExternalAiguilleur subprocess, AiguilleurManager supervisor). Couverture ≥80% sur aiguilleur/.*
