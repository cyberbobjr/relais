# Plan : Hot-Reload de la Configuration RELAIS

**Statut** : En attente de confirmation  
**Date** : 2026-04-02  

---

## Vue d'ensemble

Mécanisme hybride : **Redis Pub/Sub** pour le signal de reload, **filesystem read** dans chaque brique. L'Aiguilleur supporte la gestion dynamique complète des adaptateurs (démarrage/arrêt à chaud).

**Format du signal Pub/Sub :**
```json
{"scope": "channels|users|profiles|mcp_servers|config", "source": "admin"}
```
Canal Pub/Sub : `relais:config:reload`

---

## Phase 1 — Infrastructure commune

**Fichiers créés :**
- `common/config_watcher.py` — `ConfigReloadSubscriber` : souscrit à `relais:config:reload`, dispatch vers les callbacks par scope
- `common/config_reload_trigger.py` — `async publish_reload(redis, scope, source="admin")` pour émettre le signal

---

## Phase 2 — Aiguilleur (le plus complexe)

**`aiguilleur/core/manager.py` modifié :**
- Thread daemon `_reload_listener_thread` (Redis sync — pas asyncio) → set un `threading.Event` (`_reload_requested`)
- `_supervise()` check l'event à chaque iteration → appelle `_hot_reload_channels()` dans le thread principal (évite la race condition)
- `_hot_reload_channels()` calcule le diff avec la config courante :
  - Canal ajouté enabled → `_load_adapter()` + `start()`
  - Canal supprimé → `stop()` + retrait
  - Canal désactivé → `stop()`
  - Canal réactivé → `_load_adapter()` + `start()`
  - Canal inchangé → rien
- Chaque opération dans un `try/except` individuel (une erreur n'affecte pas les autres)

---

## Phase 3 — Briques async (4 fichiers modifiés)

| Brique | Fichier | Scopes écoutés | Actions |
|--------|---------|---------------|---------|
| **Portail** | `portail/main.py` | `users`, `config` | `UserRegistry.reload()`, `RoleRegistry.reload()`, relit `unknown_user_policy`/`guest_profile` — **fail-closed guard implémenté** : refuse rechargement permissif si config valide déjà chargée (`_config_loaded_once`) |
| **Sentinelle** | `sentinelle/main.py` | `users` | `ACLManager.reload()` — **fail-closed guard implémenté** : refuse rechargement permissif si ACL valide déjà chargée (`_config_loaded_once`) |
| **Atelier** | `atelier/main.py` | `profiles`, `mcp_servers`, `atelier` | Remplace `_profiles`, `_mcp_servers_default`, `_progress_config`; MCP restart via `_restart_mcp_sessions()` (singleton McpSessionManager remplacé atomiquement sous `_mcp_lock`) |
| **Souvenir** | `souvenir/main.py` | `channels` | Met à jour `_channels`, boucle `_process_outgoing_streams` lit `self._channels` dynamiquement |

Toutes les méthodes `_on_*_reload()` gardent l'ancienne config en cas d'erreur YAML.

---

## Phase 4 — `config.yaml` : sections hot-reloadables documentées

| Section | Hot-reload | Raison |
|---------|-----------|--------|
| `security.unknown_user_policy` | **Oui** | Relit par Portail |
| `security.guest_profile` | **Oui** | Relit par Portail |
| `llm.default_profile` | **Oui** | Déjà dynamique (pas de cache) |
| `redis.*` | **Non** | Connexions établies au démarrage |
| `litellm.*` | **Non** | URL/clé dans connexions existantes |
| `logging.*` | **Non** | Niveau configuré au démarrage |
| `paths.*` | **Non** | Répertoires résolus au démarrage |

---

## Phase 5 — CLI de déclenchement

**`scripts/reload_config.py`** (nouveau) :
```bash
PYTHONPATH=. uv run python scripts/reload_config.py --scope users
PYTHONPATH=. uv run python scripts/reload_config.py --scope all
```
- Parse `--scope` (channels|users|profiles|mcp_servers|config|all)
- Si `all` : publie un signal pour chaque scope
- Se connecte à Redis, appelle `publish_reload()`, quitte

---

## Phase 6 — Tests (5 fichiers créés)

- `tests/test_config_watcher.py` — unit : deserialization, dispatch callbacks, malformed messages, wildcard `"*"`
- `tests/test_aiguilleur_hot_reload.py` — unit : diff-and-apply des adaptateurs (mock `load_channels_config` + `_load_adapter`)
- `tests/test_portail_hot_reload.py` — unit : reload UserRegistry/RoleRegistry + config sections
- `tests/test_atelier_hot_reload.py` — unit : reload profiles + fallback si YAML invalide, reload MCP
- `tests/test_hot_reload_integration.py` — integration (`@pytest.mark.integration`) : Pub/Sub → callback, Redis réel

---

## Phase 7 — Documentation (3 fichiers modifiés/créés)

- `docs/HOT_RELOAD.md` (nouveau) — guide opérateur complet : mécanisme, CLI, matrice sections, comportement erreur
- `docs/REDIS_BUS_API.md` — ajout section Pub/Sub `relais:config:reload`
- `CLAUDE.md` — mention hot-reload dans Architecture + `common/config_watcher.py` dans Configuration & Utilities

---

## Risques

| Risque | Sévérité | Mitigation |
|--------|---------|-----------|
| YAML invalide au reload | High | try/except, ancienne config conservée, log ERROR — **implémenté** |
| Race condition Aiguilleur (`_adapters` muté depuis listener thread) | Medium | `threading.Event` — reload exécuté dans le thread principal de `_supervise()` |
| Signal Pub/Sub perdu (fire-and-forget) | Low | Operateur peut republier ; documenter l'ordre de démarrage |
| Nouveau canal Souvenir non détecté | Medium | Boucle outgoing check `self._channels` à chaque cycle |

---

## Récapitulatif des fichiers

**Fichiers créés (9) :**
- `common/config_watcher.py`
- `common/config_reload_trigger.py`
- `scripts/reload_config.py`
- `docs/HOT_RELOAD.md`
- `tests/test_config_watcher.py`
- `tests/test_aiguilleur_hot_reload.py`
- `tests/test_portail_hot_reload.py`
- `tests/test_atelier_hot_reload.py`
- `tests/test_hot_reload_integration.py`

**Fichiers modifiés (7) :**
- `aiguilleur/core/manager.py`
- `portail/main.py`
- `sentinelle/main.py`
- `atelier/main.py`
- `souvenir/main.py`
- `docs/REDIS_BUS_API.md`
- `CLAUDE.md`

---

## Critères de succès

- [ ] Modifier `channels.yaml` + trigger reload → nouveau canal démarre / canal retiré s'arrête, sans restart Aiguilleur
- [ ] Modifier `portail.yaml` + trigger reload → Portail et Sentinelle utilisent les nouvelles identités/ACL au prochain message
- [ ] Modifier `atelier/profiles.yaml` + trigger reload → Atelier utilise le nouveau profil au prochain message
- [ ] Modifier `atelier/mcp_servers.yaml` + trigger reload → Atelier utilise les nouveaux serveurs MCP au prochain message
- [ ] Modifier `security.unknown_user_policy` dans `config.yaml` + trigger reload → Portail applique la nouvelle politique
- [ ] YAML invalide pendant un reload → log ERROR, ancienne config conservée, aucun crash
- [ ] `scripts/reload_config.py --scope all` déclenche le rechargement dans toutes les briques
- [ ] Tous les tests unitaires et d'intégration passent
- [ ] Documentation `docs/HOT_RELOAD.md` complète et à jour
