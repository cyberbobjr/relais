# Plan : Hot-reload de configuration par brick

**Objectif** : Permettre à chaque brick de recharger sa configuration à chaud, sans redémarrage,
lorsqu'un fichier YAML de configuration est modifié.

**Branche suggérée** : `feat/hot-reload-config`

---

## 1. État actuel — comment chaque brick charge sa config

| Brick | Fichiers consommés | Moment du chargement | Objet rechargeable |
|---|---|---|---|
| **Aiguilleur** | `aiguilleur.yaml` | `AiguilleurManager.run()` avant `start()` | `self._adapters` (dict d'adaptateurs actifs) |
| **Portail** | `portail.yaml` | `Portail.__init__` → `UserRegistry._load()` | `self._user_registry._sender_index` / `_by_identifier` |
| **Sentinelle** | `sentinelle.yaml` | `Sentinelle.__init__` → `ACLManager._load()` | `self._acl._groups` / `_access_control` |
| **Atelier** | `atelier/profiles.yaml`, `mcp_servers.yaml`, `atelier.yaml`, `aiguilleur.yaml` | `Atelier.__init__` (4 appels) | `self._profiles`, `self._mcp_servers_default`, `self._progress_config`, `self._streaming_capable_channels` |
| **Souvenir** | `souvenir/profiles.yaml` (MemoryExtractor uniquement) | À l'appel, pas au démarrage | Aucune state durée |

**Pattern commun** : chaque brick appelle `resolve_config_path(filename)` → lit le YAML → peuple des structures en mémoire. `_load()` est déjà une fonction séparable, ce qui facilite le rechargement.

**Contrainte Aiguilleur** : `aiguilleur.yaml` contrôle quels adaptateurs sont démarrés. Un reload implique potentiellement de démarrer/arrêter des threads d'adaptateurs, pas seulement de swapper un dict → complexité structurellement plus élevée que les autres bricks.

---

## 2. Solutions proposées

### Solution A — Polling asyncio par brick (aucune dépendance nouvelle)

Chaque brick lance une tâche asyncio de fond qui compare régulièrement le `mtime` de ses fichiers de config. Si un changement est détecté, elle appelle `_reload()`.

```python
# Exemple dans Portail.start()
async def _watch_config(self) -> None:
    path = self._user_registry._config_path
    last_mtime = path.stat().st_mtime if path else 0.0
    while True:
        await asyncio.sleep(30)
        try:
            mtime = path.stat().st_mtime
            if mtime != last_mtime:
                last_mtime = mtime
                self._user_registry._load()
                logger.info("Portail: config rechargée depuis %s", path)
        except Exception as exc:
            logger.error("Portail: échec reload config: %s", exc)
```

| ✅ Avantages | ❌ Inconvénients |
|---|---|
| Zéro dépendance nouvelle | Latence jusqu'à l'intervalle de polling (ex. 30 s) |
| Chaque brick est 100 % autonome | I/O disque même si rien n'a changé |
| Implémentation triviale | Ajoute une tâche de fond à chaque brick |
| Fonctionne sur tous les OS sans adaptation | Duplication de la logique dans 4 bricks |

**Verdict** : Bon choix minimal si on veut éviter de nouvelles dépendances et qu'une latence de quelques dizaines de secondes est acceptable.

---

### Solution B — Signal SIGHUP par brick (déclenchement manuel)

Chaque brick enregistre un handler SIGHUP. L'opérateur envoie `kill -HUP <pid>` ou configure supervisord pour rediriger le signal.

```python
# Dans start() ou __init__
import signal
def _handle_sighup(signum, frame):
    logger.info("SIGHUP reçu — rechargement config")
    self._user_registry._load()
signal.signal(signal.SIGHUP, _handle_sighup)
```

| ✅ Avantages | ❌ Inconvénients |
|---|---|
| Pattern Unix standard et universel | **Non automatique** — nécessite une action opérateur |
| Zéro overhead à l'exécution | Gestion des PIDs complexe (supervisord cache les PIDs) |
| Aucune dépendance, pas de thread additionnel | Non compatible avec asyncio sans précautions (signal dans event loop) |
| Bien supporté par supervisord (`kill -HUP`) | Ne détecte pas les modifications fichier automatiquement |

**Verdict** : Utile en complément d'une autre solution (déclenchement manuel d'urgence), insuffisant seul.

---

### Solution C — `watchfiles` événementiel par brick (inotify/FSEvents)

La bibliothèque [`watchfiles`](https://watchfiles.helpmanual.io/) s'appuie sur inotify (Linux), FSEvents (macOS) ou ReadDirectoryChangesW (Windows). Elle expose une API `async for changes in awatch(path)` sans polling.

```python
# Dans Portail.start(), en parallèle de _process_stream
from watchfiles import awatch

async def _watch_config(self) -> None:
    config_dir = resolve_config_path("portail.yaml").parent
    async for _ in awatch(config_dir, watch_filter=lambda _, p: p.endswith("portail.yaml")):
        logger.info("portail.yaml modifié — rechargement")
        self._user_registry._load()
```

| ✅ Avantages | ❌ Inconvénients |
|---|---|
| Détection quasi-instantanée (< 1 ms, event-driven) | Nouvelle dépendance (`watchfiles`) |
| Intégration asyncio native | Chaque brick lance son propre watcher |
| Utilisé par FastAPI/uvicorn — mature et stable | `aiguilleur.yaml` surveillé en double (Aiguilleur + Atelier) |
| Chaque brick reste autonome | Nécessite des wrappers OS sur certains systèmes de fichiers distants (NFS) |

**Verdict** : Meilleur rapport qualité/simplicité si on accepte une dépendance supplémentaire légère. Recommandé pour Portail, Sentinelle, Atelier, Souvenir.

---

### Solution D — Brick Gardien : surveillance centralisée + Redis Pub/Sub

Nouveau process `gardien/` qui surveille tous les fichiers de config avec `watchfiles`. Lorsqu'un fichier change, il publie sur `relais:config:reload:{brick}` (Redis Pub/Sub). Chaque brick souscrit à son canal et appelle `_reload()`.

```
aiguilleur.yaml modifié
    → Gardien détecte la modification
    → PUBLISH relais:config:reload:aiguilleur
    → PUBLISH relais:config:reload:atelier
    → Aiguilleur reçoit → reload channels
    → Atelier reçoit → reload streaming_capable_channels
```

```python
# gardien/main.py (squelette)
WATCHED_FILES = {
    "portail.yaml":          ["portail"],
    "sentinelle.yaml":       ["sentinelle"],
    "aiguilleur.yaml":         ["aiguilleur", "atelier"],
    "atelier/profiles.yaml": ["atelier"],
    "mcp_servers.yaml":      ["atelier"],
    "atelier.yaml":          ["atelier"],
}

async def watch_loop(redis):
    async for changes in awatch(config_dir):
        for _, path in changes:
            for brick in WATCHED_FILES.get(Path(path).name, []):
                await redis.publish(f"relais:config:reload:{brick}", path)
```

```python
# Dans chaque brick (ex. Portail)
async def _config_reload_listener(self, redis_conn):
    async with redis_conn.pubsub() as ps:
        await ps.subscribe("relais:config:reload:portail")
        async for msg in ps.listen():
            if msg["type"] == "message":
                self._user_registry._load()
                logger.info("Portail: config rechargée via Pub/Sub Gardien")
```

| ✅ Avantages | ❌ Inconvénients |
|---|---|
| Source unique de vérité — un seul watcher pour tous les fichiers | Nouveau brick = nouveau process supervisord |
| Découplement fort : les bricks ne contiennent pas de code inotify | Si Gardien crash → plus de reload automatique (dégradation gracieuse) |
| Extensible : le canal Pub/Sub peut être utilisé pour un reload manuel (`/reload portail`) | Saut réseau Redis (~1 ms) |
| Traçabilité : Gardien peut loguer `relais:events:system` | Complexité initiale plus élevée (nouveau brick complet à écrire et tester) |
| `aiguilleur.yaml` surveillé une seule fois même s'il intéresse deux bricks | Ordering : si Portail n'est pas encore subscribed au démarrage, il rate le premier event |

**Verdict** : Solution la plus propre architecturalement. Alignée avec la philosophie micro-bricks de RELAIS. Recommandée si on vise le long terme et qu'on veut pouvoir déclencher les reloads depuis le slash command `/reload`.

---

### Solution E — Redis Pub/Sub manuel uniquement (sans surveillance de fichiers)

Pas de watcher. L'opérateur (ou un slash command `/reload <brick>`) publie sur `relais:config:reload:{brick}`. Les bricks sont déjà en écoute.

| ✅ Avantages | ❌ Inconvénients |
|---|---|
| Zéro overhead background | **Non automatique** — une modif fichier sans `/reload` reste silencieuse |
| Control total, pas de surprise | Risque d'oubli opérateur |
| Implémentation minimale côté bricks | Ne répond pas au besoin initial |
| Bonne base pour un système de reload piloté | À combiner obligatoirement avec A, C ou D |

**Verdict** : À implémenter comme couche de déclenchement manuel dans toutes les solutions, mais insuffisant seul.

---

## 3. Tableau comparatif synthétique

| Critère | A (Polling) | B (SIGHUP) | C (watchfiles/brick) | D (Gardien) | E (Manuel) |
|---|---|---|---|---|---|
| Automatique (fichier → reload) | ✅ (latence) | ❌ | ✅ (< 1 ms) | ✅ (< 1 ms) | ❌ |
| Nouvelles dépendances | ✅ aucune | ✅ aucune | ⚠️ `watchfiles` | ⚠️ `watchfiles` | ✅ aucune |
| Nouveau process | ❌ non | ❌ non | ❌ non | ⚠️ oui | ❌ non |
| Overhead runtime | ⚠️ faible | ✅ nul | ✅ nul (event) | ✅ nul (event) | ✅ nul |
| Déclenchement manuel possible | ❌ | ✅ | ❌ seul | ✅ | ✅ |
| Complexité Aiguilleur (adapters) | ⚠️ moyenne | ⚠️ moyenne | ⚠️ moyenne | ⚠️ moyenne | ⚠️ moyenne |
| Scalabilité / extensibilité | ❌ | ❌ | ⚠️ | ✅ | ⚠️ |
| Effort implémentation | Faible | Très faible | Moyen | Élevé | Très faible |

---

## 4. Recommandation

**Stratégie en deux temps :**

**Phase 1 (rapide, faible risque)** — Solution C : `watchfiles` par brick
- Portail, Sentinelle, Atelier, Souvenir chacun surveillance leur(s) fichier(s)
- Ajoute `watchfiles` comme dépendance unique
- Pas de nouveau process à gérer
- Reload atomique avec lock asyncio pour thread-safety

**Phase 2 (architecturale)** — Solution D : Gardien brick
- Centralise la surveillance
- Active le déclenchement depuis slash command `/reload <brick>`
- Migre les watchers des bricks vers le Pub/Sub

> Si l'équipe préfère rester sans nouvelle dépendance, **Solution A** est acceptable pour
> une implémentation immédiate avec une latence de polling de 30 s.

---

## 5. Cas particulier : Aiguilleur et `aiguilleur.yaml`

L'Aiguilleur ne se contente pas de lire des données — `aiguilleur.yaml` détermine quels adaptateurs (threads) sont actifs. Un reload a trois cas possibles :

1. **Canal `enabled` → `enabled`** : paramètres changés → stop + restart de l'adaptateur
2. **Canal `disabled` → `enabled`** : créer et démarrer le nouvel adaptateur
3. **Canal `enabled` → `disabled`** : stopper et supprimer l'adaptateur

`AiguilleurManager` a déjà `_stop_all()`, `_load_adapter()`, `_supervise()`. Il faudra ajouter une méthode `_apply_config_diff(old_cfg, new_cfg)` qui reconcilie les différences.

```python
def _apply_config_diff(self, new_configs: dict[str, ChannelConfig]) -> None:
    old_names = set(self._adapters)
    new_names = {n for n, c in new_configs.items() if c.enabled}

    # Stop removed/disabled adapters
    for name in old_names - new_names:
        self._adapters[name].stop(timeout=8.0)
        del self._adapters[name]

    # Start added/enabled adapters
    for name in new_names - old_names:
        adapter = self._load_adapter(name, new_configs[name])
        self._adapters[name] = adapter
        adapter.start()

    # Restart changed adapters (config drift)
    for name in old_names & new_names:
        if self._adapters[name].config != new_configs[name]:
            self._adapters[name].stop(timeout=8.0)
            adapter = self._load_adapter(name, new_configs[name])
            self._adapters[name] = adapter
            adapter.start()
```

---

## 6. Plan d'implémentation (Solution C recommandée — Phase 1)

### Étape 0 — Socle commun : `common/config_watcher.py`

**Contexte** : Créer un helper réutilisable qui encapsule la logique de surveillance et de reload.

**Tâches** :
- [ ] Ajouter `watchfiles` à `pyproject.toml` (`poetry add watchfiles`)
- [ ] Créer `common/config_watcher.py` : classe `ConfigWatcher(files: list[Path], on_change: Callable)`
  - Méthode async `watch()` : `async for changes in awatch(...)` → appel `on_change(path)`
  - Gestion des exceptions avec log + retry
  - Lock asyncio pour éviter les reloads concurrents
- [ ] Tests unitaires `tests/test_config_watcher.py`

**Vérification** : `pytest tests/test_config_watcher.py -v`

**Exit criteria** : `ConfigWatcher` testée, détecte un changement de fichier en < 200 ms dans les tests

---

### Étape 1 — Portail : reload de `portail.yaml`

**Contexte** : `Portail.__init__` crée `UserRegistry()` qui appelle `_load()`. Pour recharger, il suffit de rappeler `_load()` sous un lock.

**Fichiers modifiés** : `portail/main.py`, `portail/user_registry.py`

**Tâches** :
- [ ] Ajouter `reload()` à `UserRegistry` : acquiert un `asyncio.Lock`, appelle `_load()`, swaps atomique des index
- [ ] Dans `Portail.start()` : lancer `ConfigWatcher([portail_yaml_path], self._user_registry.reload)` comme tâche asyncio parallèle
- [ ] Tests : `tests/test_portail_hot_reload.py`

**Vérification** :
```bash
pytest tests/test_portail_hot_reload.py -v
# Modifier portail.yaml manuellement et vérifier que reload est loggé
```

**Exit criteria** : Un changement dans `portail.yaml` déclenche un reload loggé sans interruption du traitement des messages

---

### Étape 2 — Sentinelle : reload de `sentinelle.yaml`

**Contexte** : Même pattern que Portail. `ACLManager._load()` est déjà isolé.

**Fichiers modifiés** : `sentinelle/main.py`, `sentinelle/acl.py`

**Tâches** :
- [ ] Ajouter `reload()` à `ACLManager` (lock + `_load()`)
- [ ] Dans `Sentinelle.start()` : lancer `ConfigWatcher([sentinelle_yaml_path], self._acl.reload)`
- [ ] Tests : `tests/test_sentinelle_hot_reload.py`

**Vérification** : `pytest tests/test_sentinelle_hot_reload.py -v`

**Exit criteria** : Idem Portail

---

### Étape 3 — Atelier : reload de 4 fichiers

**Contexte** : Atelier charge 4 sources distinctes dans `__init__`. Chaque source a son propre objet cible. Le reload doit être atomique pour éviter qu'un message en cours de traitement voie une config partiellement rechargée.

**Fichiers modifiés** : `atelier/main.py`

**Tâches** :
- [ ] Ajouter `_reload_profiles()`, `_reload_mcp_servers()`, `_reload_progress_config()`, `_reload_channels()` sur `Atelier`
- [ ] Protéger avec `asyncio.Lock` (un lock global ou par ressource)
- [ ] `ConfigWatcher` avec mapping fichier → callback :
  ```python
  watchers = [
      ConfigWatcher(["atelier/profiles.yaml"], self._reload_profiles),
      ConfigWatcher(["mcp_servers.yaml"], self._reload_mcp_servers),
      ConfigWatcher(["atelier.yaml"], self._reload_progress_config),
      ConfigWatcher(["aiguilleur.yaml"], self._reload_channels),
  ]
  ```
- [ ] Tests : `tests/test_atelier_hot_reload.py`

**Vérification** : `pytest tests/test_atelier_hot_reload.py -v`

**Exit criteria** : Chaque fichier déclenche uniquement le reload de sa ressource cible

---

### Étape 4 — Aiguilleur : reload de `aiguilleur.yaml` avec reconciliation

**Contexte** : Cas le plus complexe. Voir section 5 ci-dessus.

**Fichiers modifiés** : `aiguilleur/core/manager.py`

**Tâches** :
- [ ] Implémenter `_apply_config_diff(new_configs)` sur `AiguilleurManager`
- [ ] Ajouter méthode `reload_channels()` : charge new cfg → diff → reconcile
- [ ] Lancer `ConfigWatcher([channels_yaml_path], self.reload_channels)` dans `run()` (via `threading` car `run()` n'est pas asyncio)
  - Alternative : faire tourner `watchfiles.watch()` (API synchrone) dans un thread séparé
- [ ] Tests : `tests/test_aiguilleur_hot_reload.py`

**Vérification** : `pytest tests/test_aiguilleur_hot_reload.py -v`

**Exit criteria** : Activer/désactiver un canal dans `aiguilleur.yaml` démarre/arrête l'adaptateur sans toucher les autres

---

### Étape 5 — Souvenir : reload optionnel

**Contexte** : Souvenir n'a pas de config critique rechargeable à chaud — le `MemoryExtractor` relit le profil à chaque appel. Aucune action requise en Phase 1.

**Tâches** :
- [ ] Vérifier que `MemoryExtractor` n'a pas de config en cache qui deviendrait stale
- [ ] Si besoin : ajouter un reload sur `souvenir/profiles.yaml` en suivant le même pattern

---

### Étape 6 — Tests d'intégration + documentation

**Tâches** :
- [ ] Test d'intégration : modifier chaque fichier YAML et vérifier le reload dans un environnement supervisord complet
- [ ] Mettre à jour `docs/ARCHITECTURE.md` : section "Configuration hot-reload"
- [ ] Mettre à jour `CLAUDE.md` : noter que `ConfigWatcher` est le mécanisme standard
- [ ] Mettre à jour `docs/REDIS_BUS_API.md` si Phase 2 (Gardien) est activée

---

## 7. Invariants à respecter dans toute implémentation

1. **Atomicité** : Jamais de swap partiel visible par `_handle_message`. Utiliser un `asyncio.Lock` ou reconstruire l'objet entier puis assigner la référence en une opération.
2. **Pas de perte de messages** : Le reload ne doit jamais interrompre le `_process_stream` loop ni provoquer un timeout PEL.
3. **Dégradation gracieuse** : Si le fichier est invalide au moment du reload (YAML cassé), logger un CRITICAL et conserver l'ancienne configuration.
4. **Traçabilité** : Chaque reload doit générer un log `INFO` avec le fichier rechargé, et idéalement un event dans `relais:events:system`.
5. **Thread-safety pour Aiguilleur** : `AiguilleurManager.run()` est synchrone/multi-thread. Utiliser un `threading.Lock` (pas asyncio) pour protéger `_adapters`.

---

## 8. Dépendances et risques

| Risque | Mitigation |
|---|---|
| `watchfiles` non disponible sur certains environnements (ex. container minimal) | Fallback polling si `ImportError watchfiles` — implémenter le pattern defender dès le départ |
| Reload d'Aiguilleur laisse un adaptateur dans un état incohérent | `_apply_config_diff` doit toujours stopper proprement avant de recréer |
| Deux reloads concurrents si le fichier est sauvegardé plusieurs fois en rafale | `asyncio.Lock` + debounce 500 ms dans `ConfigWatcher` |
| Reload en plein milieu d'un appel LLM (Atelier) | Le lock ne protège que l'accès à `self._profiles`, pas l'exécuteur en cours — l'exécuteur utilise sa propre copie locale de `profile` déjà résolue |

---

*Plan généré le 2026-04-04 — basé sur l'analyse de `aiguilleur/`, `portail/`, `sentinelle/`, `atelier/`, `souvenir/`, `common/config_loader.py`*
