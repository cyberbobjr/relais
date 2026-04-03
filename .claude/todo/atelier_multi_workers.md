# Plan : Multi-Worker Support pour Atelier

**Statut : EN ATTENTE D'IMPLÉMENTATION**
**Date de création : 2026-03-31**

## Objectif

Permettre à Atelier de traiter plusieurs requêtes LLM simultanément dans le même process asyncio,
en lançant N workers, chacun avec son propre `consumer_name` dans le consumer group Redis `atelier_group`.
Contrôlé via `ATELIER_WORKERS` (défaut : 1 — rétrocompat totale).

---

## Phase 1 — Changements core `atelier/main.py` (3 méthodes)

### Étape 1 — `__init__` : supprimer `self.consumer_name`

- Supprimer : `self.consumer_name: str = "atelier_1"`
- Ajouter : `self._consumer_prefix: str = os.environ.get("ATELIER_CONSUMER_PREFIX", "atelier")`
- Ajouter : `self._num_workers: int` = `int(os.environ.get("ATELIER_WORKERS", "1"))` clampé à `[1, 32]`
  - Warning log si valeur hors bornes ou non-entière (défaut à 1)

### Étape 2 — `_process_stream` : ajouter `worker_id: int = 1`

Nouvelle signature :
```python
async def _process_stream(
    self, redis_conn: Any, shutdown: GracefulShutdown | None = None, *, worker_id: int = 1
) -> None:
```

- `consumer_name = f"{self._consumer_prefix}_{worker_id}"` (variable locale, plus `self.consumer_name`)
- Remplacer toutes les références à `self.consumer_name` par la variable locale
- Mettre à jour le log de démarrage : `"Atelier worker %d (%s) listening for tasks..."`

### Étape 3 — `start()` : spawner N workers avec `asyncio.gather`

```python
async def start(self) -> None:
    shutdown = GracefulShutdown()
    shutdown.install_signal_handlers()
    redis_conn = await self.client.get_connection()
    await redis_conn.xadd("relais:logs", {
        "level": "INFO", "brick": "atelier",
        "message": f"Atelier starting with {self._num_workers} worker(s)"
    })
    try:
        workers = [
            self._process_stream(redis_conn, shutdown=shutdown, worker_id=i)
            for i in range(1, self._num_workers + 1)
        ]
        await asyncio.gather(*workers)
    except asyncio.CancelledError:
        logger.info("Atelier shutting down...")
    finally:
        await self.client.close()
        logger.info("Atelier stopped gracefully")
```

---

## Phase 2 — Nouveaux tests (`tests/test_atelier.py`)

5 nouveaux tests à ajouter :

1. `test_single_worker_default_consumer_name`
   - Sans `ATELIER_WORKERS`, xreadgroup appelé avec `"atelier_1"`

2. `test_multi_worker_distinct_consumer_names`
   - `ATELIER_WORKERS=3` → consumer names `{"atelier_1", "atelier_2", "atelier_3"}`

3. `test_all_workers_stop_on_shutdown`
   - `ATELIER_WORKERS=2` + `_PreSetShutdown` → `start()` se termine proprement

4. `test_custom_consumer_prefix`
   - `ATELIER_CONSUMER_PREFIX=gpu_atelier` + `ATELIER_WORKERS=2`
   - → consumer names `"gpu_atelier_1"` et `"gpu_atelier_2"`

5. `test_invalid_workers_env_clamped`
   - `ATELIER_WORKERS=0` (ou `"abc"`) → `_num_workers == 1` + warning log

---

## Phase 3 — Adaptation tests existants (`tests/test_shutdown_wiring.py`)

2 tests à adapter (`test_atelier_exits_on_shutdown` et `test_atelier_calls_install_signal_handlers`) :

```python
# Avant
atelier.consumer_name = "atelier_1"

# Après
atelier._consumer_prefix = "atelier"
atelier._num_workers = 1
```

Les autres tests `test_atelier.py` ne nécessitent pas de modification (worker_id défaut = 1 → consumer = `"atelier_1"`).

---

## Phase 4 — Documentation

| Fichier | Changement |
|---|---|
| `README.md` | Ajouter `ATELIER_WORKERS` et `ATELIER_CONSUMER_PREFIX` dans la table des env vars |
| `CLAUDE.md` | Section Atelier : consumer_name dynamique, exemple `ATELIER_WORKERS=4 PYTHONPATH=. uv run python atelier/main.py` |
| `docs/ARCHITECTURE.md` | Section "Single Atelier instance" (l.584) : remplacer l'exemple shell N instances par `ATELIER_WORKERS`. Mettre à jour la limite perf qui est maintenant scalable in-process |
| `docs/ENV.md` | Ajouter `ATELIER_WORKERS` et `ATELIER_CONSUMER_PREFIX` dans la table |
| `plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md` | Section Atelier : mentionner le multi-worker et `ATELIER_WORKERS` |

`docs/REDIS_BUS_API.md` et `docs/CONTRIBUTING.md` : pas impactés.

---

## Risques identifiés

| Risque | Mitigation |
|---|---|
| `asyncio.gather` + CancelledError : si un worker plante, tous s'arrêtent | Comportement souhaité — déjà géré par `except asyncio.CancelledError` |
| `_fetch_context` + N workers : O(N×M) sur lecture Souvenir | Safe (filtrage par `correlation_id`) — documenter comme limite pour N > 8 |
| `xgroup_create` appelé N fois en parallèle | Déjà idempotent (`BUSYGROUP` catchée), aucun changement |
| Contention connexion Redis partagée | asyncio single-threaded → pas de vraie contention |

---

## Critères de succès

- [ ] `ATELIER_WORKERS` non défini → comportement identique à aujourd'hui (1 worker, `atelier_1`)
- [ ] `ATELIER_WORKERS=4` → 4 workers `atelier_1..atelier_4` en parallèle
- [ ] Tous les tests existants passent (14 `test_atelier.py` + 6 shutdown wiring)
- [ ] 5 nouveaux tests passent
- [ ] Shutdown propre : tous les workers s'arrêtent sur SIGTERM
- [ ] Valeurs invalides (`0`, `-1`, `abc`) → défaut 1 + warning log
