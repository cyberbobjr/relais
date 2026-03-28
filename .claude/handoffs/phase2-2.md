# Handoff Phase 2.2 — Fix XACK inconditionnel + Résilience LiteLLM

## Bug corrigé

**Symptôme :** toute tâche arrivant pendant un restart LiteLLM (ConnectError ou Timeout) était
définitivement perdue sans erreur visible.

**Cause racine :** dans `atelier/main.py`, le bloc `finally: await redis_conn.xack(...)` était
inconditionnel. Sur `httpx.ConnectError` :
1. L'exception était catchée et loguée
2. Le `finally` s'exécutait immédiatement → XACK envoyé
3. Le message quittait définitivement le stream — tâche perdue

**Règle fondamentale appliquée :** ne jamais XACK avant le succès ou l'épuisement des retries.

---

## Fichiers modifiés / créés

### Créé : `atelier/executor.py`

Nouveau module responsable des appels LiteLLM avec résilience :

- `RETRIABLE = (httpx.ConnectError, httpx.TimeoutException)` — tuple utilisable en `except`
- `RETRY_DELAYS = [2, 5, 15]` — backoff progressif en secondes
- `ExhaustedRetriesError` — exception levée après épuisement des 3 tentatives
- `execute_with_resilience(http_client, envelope, context, litellm_url, model) -> str`
  - Retente sur `ConnectError`, `TimeoutException`, HTTP 502/503/504
  - Lève `ExhaustedRetriesError` après épuisement
  - Lève l'exception originale immédiatement pour les erreurs non-retriable (400, 401, etc.)
  - Log chaque tentative en WARNING avec `correlation_id`

### Modifié : `atelier/main.py`

Changements principaux :

1. **Imports ajoutés :** `time`, `from atelier import executor`, `from atelier.executor import ExhaustedRetriesError`
2. **`self.litellm_url`** corrigé : `http://localhost:4000/v1` (sans `/chat/completions` — géré par executor)
3. **`self.litellm_model`** extrait en attribut de classe
4. **Appel LiteLLM** remplacé par `executor.execute_with_resilience(...)`
5. **Pattern XACK conditionnel** :
   - `success = False` avant le try
   - `success = True` seulement après publication réussie sur le stream de sortie
   - `ExhaustedRetriesError` → DLQ `relais:tasks:failed` + `success = True` (ACK car message dans DLQ)
   - `executor.RETRIABLE` → **pas d'ACK** (message reste dans PEL pour re-livraison)
   - `Exception` générique → ACK (pour éviter de polluer le PEL avec des messages non-récupérables)
   - `finally: if success: await redis_conn.xack(...)`

---

## Dead Letter Queue

Stream Redis : `relais:tasks:failed`

Format d'entrée :
```
{
  "payload": "<envelope_json>",
  "reason": "<ExhaustedRetriesError message>",
  "failed_at": "<unix timestamp float>"
}
```

L'Archiviste observe déjà plusieurs streams — il devra être configuré pour observer
`relais:tasks:failed` et logger au niveau ERROR.

---

## Tests critiques à écrire

### Unit — `tests/atelier/test_executor.py`

| Test | Description |
|------|-------------|
| `test_success_first_attempt` | Appel LiteLLM réussit au premier essai, retourne le texte |
| `test_retry_on_connect_error` | ConnectError → retry × 2 puis succès, vérifie les sleeps |
| `test_retry_on_timeout` | TimeoutException → même comportement |
| `test_retry_on_502` | HTTP 502 → retry, succès au 2e essai |
| `test_exhausted_retries_raises` | 3× ConnectError → lève ExhaustedRetriesError |
| `test_non_retriable_400_raises_immediately` | HTTP 400 → lève HTTPStatusError sans retry |
| `test_non_retriable_401_raises_immediately` | HTTP 401 → lève HTTPStatusError sans retry |
| `test_retry_delays_respected` | Vérifie que asyncio.sleep est appelé avec [2, 5] sur 3 tentatives |

### Integration — `tests/atelier/test_main_xack.py`

| Test | Description |
|------|-------------|
| `test_success_xack_sent` | Traitement nominal → xack appelé exactement 1× |
| `test_connect_error_no_xack` | ConnectError directe → xack NON appelé, message dans PEL |
| `test_exhausted_retries_dlq_and_xack` | ExhaustedRetriesError → message dans `relais:tasks:failed` + xack appelé |
| `test_non_retriable_error_xack_sent` | Exception générique (ex: JSON invalide) → xack appelé (évite PEL poison) |

### Fixtures recommandées

```python
# Mock httpx.AsyncClient avec respx ou unittest.mock
# Mock redis_conn avec AsyncMock
# Utiliser Envelope.from_json(envelope_fixture) pour les données de test
```

---

## Impact sur les autres briques

| Brique | Impact |
|--------|--------|
| **Archiviste** | Doit être configuré pour consommer `relais:tasks:failed` et alerter niveau ERROR. Pas de changement de code immédiat mais à planifier Phase 2.6. |
| **Souvenir** | Aucun — l'interface `relais:memory:request/response` est inchangée. |
| **Portail / Sentinelle** | Aucun — le stream `relais:tasks` en entrée est inchangé. |
| **Aiguilleur** | Aucun — le stream `relais:messages:outgoing:{channel}` en sortie est inchangé. |
| **Veilleur (futur)** | Le stream `relais:tasks:failed` est le point d'entrée pour le rejeu manuel prévu Phase 4.2. |
| **Scrutateur (futur)** | Pourra exposer une métrique `atelier_dlq_total` en observant `relais:tasks:failed`. |

---

## Note d'architecture

Le comportement choisi pour les erreurs génériques (`except Exception`) est d'ACK (éviter le poison
de PEL). Ce choix est conservateur et correct pour les erreurs de parsing (JSON invalide, envelope
corrompue) mais pourrait être raffiné plus tard en distinguant les erreurs de données des erreurs
d'infrastructure. Pour l'instant, toute erreur non-réseau conduit à un ACK + log ERROR.
