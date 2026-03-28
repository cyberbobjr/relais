# Phase 1 Handoff — common/ consolidation

Date: 2026-03-27

---

## Fichiers créés

| Fichier | Lignes | Description |
|---------|--------|-------------|
| `common/shutdown.py` | ~110 | GracefulShutdown — gestion SIGTERM/SIGINT via asyncio.Event |
| `common/stream_client.py` | ~145 | StreamConsumer + StreamProducer — abstraction XREADGROUP/XADD |
| `common/event_publisher.py` | ~55 | EventPublisher — Pub/Sub fire-and-forget sur `relais:events:*` |
| `common/health.py` | ~65 | `health()` — dict standard `{status, brick, uptime_seconds, redis}` |
| `common/markdown_converter.py` | ~175 | `convert_md_to_telegram`, `convert_md_to_slack_mrkdwn`, `strip_markdown` |

---

## Patterns notables

### shutdown.py
- `asyncio.Event` comme signal d'arrêt partageable (`shutdown.stop_event`) — les boucles worker peuvent faire `await shutdown.stop_event.wait()` au lieu de `while True`.
- `install_signal_handlers()` à appeler depuis la coroutine principale après `asyncio.get_event_loop()`.
- `wait_for_tasks()` utilise `asyncio.wait_for(gather(...), timeout=30)` puis force-cancel les stragglers.
- Timeout configurable via le paramètre `timeout` (défaut = `SHUTDOWN_TIMEOUT_SECONDS = 30`).

### stream_client.py
- `StreamConsumer.consume(callback)` ne fait **jamais** de XACK automatique — le callback est responsable d'appeler `await consumer.ack(msg_id)` après traitement réussi. C'est intentionnel pour rester compatible avec le fix XACK conditionnel de Phase 2.2.
- `create_group()` gère BUSYGROUP silencieusement (pattern identique à tous les `main.py` existants).
- `MessageCallback = Callable[[str, dict[str, str]], Awaitable[None]]` — type alias exportable pour les hints dans les briques.

### event_publisher.py
- Inject automatique de `timestamp` (time.time()) et `event_type` dans chaque payload.
- Le champ `event_type` dans le payload permet aux subscribers de démultiplexer sans parser le nom du channel.

### health.py
- `_START_TIME = time.monotonic()` capturé à l'import du module → uptime réel du processus.
- Retourne `"degraded"` uniquement si Redis est fourni **et** inaccessible. Pas de Redis fourni → `"n/a"` et `"ok"`.

### markdown_converter.py
- Traitement des blocs ``` en premier (split avant toute substitution) pour éviter d'altérer le code.
- `_escape_telegram()` escape les 19 caractères spéciaux MarkdownV2 listés dans la doc Telegram Bot API.
- `convert_md_to_telegram()` réapplique l'escape sur les spans de texte brut après substitution des balises — risque de double-escape si du Markdown complexe est imbriqué (voir Questions ouvertes).

---

## Tests à écrire

### common/shutdown.py
- `test_register_and_cancel`: enregistrer une tâche infinie, appeler `signal_handler(SIGTERM)`, vérifier qu'elle est annulée.
- `test_wait_for_tasks_clean`: tâches qui se terminent dans le délai → aucun force-cancel.
- `test_wait_for_tasks_timeout`: tâche qui ne se termine pas → force-cancel après timeout.
- `test_stop_event_set_on_signal`: `is_stopping()` retourne True après `signal_handler()`.

### common/stream_client.py
- `test_create_group_busygroup_ignored`: simuler une exception BUSYGROUP → pas de re-raise.
- `test_create_group_other_error_raised`: exception non-BUSYGROUP → re-raise.
- `test_consume_calls_callback`: injecter des messages mockés, vérifier que callback est appelé avec `(msg_id, data)`.
- `test_consume_no_autoack`: vérifier que `xack` n'est PAS appelé automatiquement par `consume()`.
- `test_publish_returns_msg_id`: `StreamProducer.publish()` retourne la valeur de `xadd`.

### common/event_publisher.py
- `test_emit_publishes_on_correct_channel`: vérifier channel = `relais:events:{event_type}`.
- `test_emit_injects_timestamp`: le payload JSON contient un champ `timestamp`.
- `test_emit_injects_event_type`: le payload JSON contient `event_type`.

### common/health.py
- `test_health_no_redis`: `redis=None` → `{"redis": "n/a", "status": "ok"}`.
- `test_health_redis_ok`: mock Redis.ping() sans exception → `{"redis": "ok", "status": "ok"}`.
- `test_health_redis_error`: mock Redis.ping() qui lève → `{"redis": "error", "status": "degraded"}`.
- `test_health_uptime_positive`: `uptime_seconds` > 0.

### common/markdown_converter.py
- `test_telegram_bold`: `**text**` → `*text*` (avec escapes).
- `test_telegram_code_block_untouched`: contenu entre ``` non modifié.
- `test_telegram_special_chars_escaped`: `.` `!` `(` etc. échappés dans texte brut.
- `test_slack_link`: `[label](url)` → `<url|label>`.
- `test_slack_bold`: `**text**` → `*text*`.
- `test_strip_removes_all_formatting`: texte sans aucun caractère Markdown après `strip_markdown`.
- `test_strip_keeps_link_text`: `[label](url)` → `label`.

---

## Questions ouvertes

1. **Double-escape Telegram** — `convert_md_to_telegram()` applique `_escape_telegram()` dans les lambdas de remplacement, puis un second `re.sub` sur le segment entier pour les caractères résiduels. Du Markdown imbriqué complexe (ex: lien avec parenthèses dans l'URL) peut produire des doubles-backslashes. À valider avec des cas réels depuis Le Crieur / Aiguilleur Telegram.

2. **`StreamConsumer.create_group()` — id="0" vs "$"** — L'id `"0"` relit les messages non-consommés depuis le début du stream (comportement idempotent au redémarrage). Si une brique neuve ne doit lire que les nouveaux messages, passer `id="$"`. À paramétrer ou documenter selon la brique.

3. **`health()` — uptime multi-process** — `_START_TIME` est capturé à l'import de `common.health`. Si plusieurs briques sont dans le même processus (mode dev monolithe), l'uptime sera partagé. En production (supervisord, un process par brique), c'est correct.

4. **`GracefulShutdown.install_signal_handlers()`** — Ne fonctionne pas depuis un thread non-principal (`signal` ne peut être installé que depuis le thread principal). Si une brique utilise `run_in_executor()` ou des threads, prévoir un mécanisme alternatif (ex: `loop.call_soon_threadsafe`).

5. **`EventPublisher` — pas de sérialisation des types non-JSON** — `json.dumps(payload)` lèvera `TypeError` si `data` contient des types non-sérialisables (datetime, Enum, etc.). Ajouter un `default=str` ou un encodeur custom si nécessaire.
