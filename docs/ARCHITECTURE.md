# RELAIS — Architecture Technique

**Dernière mise à jour :** 2026-04-10

Ce document décrit l'architecture effectivement implémentée dans le code du dépôt.

---

## Briques actives

| Brique | Rôle | Entrée principale |
|--------|------|-------------------|
| `aiguilleur` | Adaptateurs de canaux entrants/sortants | `aiguilleur/main.py` |
| `portail` | Validation d'enveloppe et enrichissement identité | `portail/main.py` |
| `sentinelle` | ACL, routage entrant et sortant | `sentinelle/main.py` |
| `atelier` | Exécution LLM via DeepAgents/LangGraph | `atelier/main.py` |
| `commandant` | Commandes slash hors LLM | `commandant/main.py` |
| `souvenir` | Mémoire Redis + SQLite | `souvenir/main.py` |
| `archiviste` | Logs et observation partielle du pipeline | `archiviste/main.py` |
| `forgeron` | Amélioration autonome des skills via analyse LLM des traces | `forgeron/main.py` |

---

## Flux de données

```text
Utilisateur
  -> Aiguilleur
  -> relais:messages:incoming
  -> Portail
  -> relais:security
  -> Sentinelle
     -> relais:tasks -> Atelier
     -> relais:commands -> Commandant

Atelier
  -> relais:skill:trace -> Forgeron
     -> relais:events:system (patch_applied / patch_rolled_back)
  -> relais:messages:streaming:{channel}:{correlation_id}
  -> relais:messages:outgoing_pending -> Sentinelle sortant -> relais:messages:outgoing:{channel}
  -> relais:messages:outgoing:{channel} pour certains progress events
  -> relais:tasks:failed en cas d'échec non récupérable
  (historique conversationnel géré par LangGraph checkpointer AsyncSqliteSaver — checkpoints.db)

Commandant
  -> relais:messages:outgoing:{channel} pour /help
  -> relais:memory:request pour /clear

Souvenir
  observe relais:messages:outgoing:{channel}

Aiguilleur
  consomme relais:messages:outgoing:{channel}
  -> canal externe
```

---

## Streams Redis

### Pipeline principal

| Stream | Producteur | Consommateur |
|--------|------------|--------------|
| `relais:messages:incoming` | Aiguilleur | Portail |
| `relais:security` | Portail | Sentinelle |
| `relais:tasks` | Sentinelle | Atelier |
| `relais:commands` | Sentinelle | Commandant |
| `relais:messages:outgoing_pending` | Atelier | Sentinelle |
| `relais:messages:outgoing:{channel}` | Sentinelle, Atelier, Commandant | Aiguilleur |

### Mémoire

| Stream / clé | Producteur | Consommateur |
|--------------|------------|--------------|
| `relais:memory:request` | Atelier, Commandant | Souvenir (`souvenir_group`), Forgeron (`forgeron_archive_group`) |
| `relais:memory:response` | Souvenir | agents (via SouvenirBackend) |

### Amélioration autonome (Forgeron)

| Stream / clé | Producteur | Consommateur |
|--------------|------------|--------------|
| `relais:skill:trace` | Atelier | Forgeron (`forgeron_group`) — trace analysis pipeline |
| `relais:memory:request` | Atelier | Forgeron (`forgeron_archive_group`) — auto-skill creation pipeline, Souvenir (`souvenir_group`) |
| `relais:events:system` | Forgeron | Archiviste |
| `relais:messages:outgoing_pending` | Forgeron (notifications) | Sentinelle |
| `relais:skill:annotation_cooldown:{skill_name}` (Redis String) | Forgeron | Forgeron (Phase 1 changelog cooldown) |
| `relais:skill:consolidation_cooldown:{skill_name}` (Redis String) | Forgeron | Forgeron (Phase 2 consolidation cooldown) |
| `relais:skill:creation_cooldown:{intent_label}` (Redis String) | Forgeron | Forgeron (auto-creation cooldown) |

### Streaming et erreurs

| Stream | Producteur | Consommateur |
|--------|------------|--------------|
| `relais:messages:streaming:{channel}:{correlation_id}` | Atelier | adaptateur de canal streaming |
| `relais:tasks:failed` | Atelier | observation / diagnostic |
| `relais:messages:outgoing:failed` | adaptateurs Aiguilleur | observation / diagnostic — DLQ pour les messages sortants non livrables (`STREAM_OUTGOING_FAILED`) |
| `relais:admin:pending_users` | Portail | revue manuelle |
| `relais:logs` | toutes les briques | Archiviste |
| `relais:events:messages` | divers | Archiviste |

### Clés Redis spécifiques canal

| Clé | Producteur | Consommateur | Description |
|-----|------------|--------------|-------------|
| `relais:whatsapp:pairing` (Redis String JSON, TTL 300s) | `scripts/pair_whatsapp.py` | Adaptateur WhatsApp / opérateur | Contexte de pairing QR actif (`KEY_WHATSAPP_PAIRING`) |

---

## BrickBase — infrastructure commune

Toutes les briques du pipeline principal (`portail`, `sentinelle`, `atelier`, `souvenir`, `commandant`, `forgeron`) héritent de `common.brick_base.BrickBase`. Cette classe abstraite fournit :

| Mécanisme | Description |
|-----------|-------------|
| `start()` | Point d'entrée unifié : connexion Redis → `on_startup()` → boucles stream concurrentes → `on_shutdown()` |
| `_run_stream_loop(spec, redis, shutdown_event)` | Boucle XREADGROUP avec gestion XACK conditionnelle (`ack_mode="always"\|"on_success"`) |
| `reload_config()` | Rechargement atomique via `safe_reload` (parse → lock → swap) |
| `_start_file_watcher()` | Surveille `_config_watch_paths()` via `watchfiles` |
| `_config_reload_listener()` | Écoute `relais:config:reload:{brick}` en Pub/Sub |
| `_create_shutdown()` | Instancie `GracefulShutdown` — les sous-classes surchargent pour la patchabilité des tests |
| `_extra_lifespan(stack)` | Hook pour entrer des context managers supplémentaires (ex. `AsyncSqliteSaver` dans Atelier) |
| `configure_logging_once()` | Fonction module-level : configure `logging.basicConfig` une seule fois. Priorité : env `LOG_LEVEL` > `config.yaml` `logging.level` (via `get_log_level()`) > `"INFO"` |

Chaque brique déclare ses flux via `stream_specs() -> list[StreamSpec]` et son handler `async (envelope, redis) -> bool`.

---

## Comportement par brique

### Aiguilleur

- Charge les canaux via `load_channels_config()`. En l'absence de `config/aiguilleur.yaml` (fichier supprimé manuellement ou répertoire non initialisé), un WARNING est loggué et le code retombe sur un fallback `discord` minimal.
- Démarre un adaptateur par canal activé.
- Adaptateurs natifs Python complets présents dans le dépôt :
  - **Discord** (`aiguilleur/channels/discord/adapter.py`) — entrée `relais:messages:incoming`, sortie `relais:messages:outgoing:discord`.
  - **WhatsApp** (`aiguilleur/channels/whatsapp/adapter.py`) — serveur webhook aiohttp écoutant la passerelle externe [fazer-ai/baileys-api](https://github.com/fazer-ai/baileys-api) (Node.js, lancée par `scripts/run_baileys.py` sous supervisord, programme `baileys-api` dans le groupe `optional`). L'adaptateur transcrit les events WhatsApp entrants en Envelope → `relais:messages:incoming`, et envoie les réponses sortantes via l'API REST de la passerelle après conversion Markdown→WhatsApp (`common/markdown_converter.convert_md_to_whatsapp()` pour `*bold*`, `_italic_`, `~strike~` natifs).
- Chaque adaptateur estampille `context.aiguilleur["channel_profile"]` depuis `ChannelConfig.profile` (aiguilleur.yaml).
- Chaque adaptateur estampille `context.aiguilleur["channel_prompt_path"]` depuis `ChannelConfig.prompt_path` (aiguilleur.yaml). `None` si non configuré — aucun overlay de canal n'est chargé.
- Chaque adaptateur estampille `context.aiguilleur["streaming"]` (`bool`) depuis `ChannelConfig.streaming` (lu par Atelier par message, pas au démarrage).

### Portail

- Consomme `relais:messages:incoming`.
- Valide l'enveloppe.
- Résout l'utilisateur avec `UserRegistry`.
- Écrit dans `context.portail`: `user_record`, `user_id` et `llm_profile` (depuis `context.aiguilleur["channel_profile"]` ou `"default"`).
- Applique `unknown_user_policy` :
  - `deny` : drop silencieux
  - `guest` : stamp guest puis forward
  - `pending` : écrit sur `relais:admin:pending_users` puis drop
- Publie sur `relais:security`.

### Sentinelle

- Entrant :
  - consomme `relais:security`
  - applique ACL
  - route vers `relais:tasks` ou `relais:commands`
  - répond inline pour commande inconnue ou non autorisée
- Sortant :
  - consomme `relais:messages:outgoing_pending`
  - fait aujourd'hui un pass-through vers `relais:messages:outgoing:{channel}`

### Atelier

- Consomme `relais:tasks`.
- Gère l'historique conversationnel via un checkpointer LangGraph persistant (`AsyncSqliteSaver`, `checkpoints.db`). L'ID de thread est `user_id` (stable cross-session).
- Assemble le prompt système avec `SoulAssembler`.
- Pour chaque appel, `AgentExecutor` préfixe le premier message utilisateur par un bloc `<relais_execution_context>` qui contient `sender_id`, `channel`, `session_id`, `correlation_id` et `reply_to` extraits de l'enveloppe. Ce bloc est strictement de la métadonnée technique — les skills (notamment `channel-setup` pour le pairing WhatsApp) peuvent le lire pour router correctement leurs actions, et le prompt système demande au modèle de **ne pas** le renvoyer à l'utilisateur.
- Exécute `AgentExecutor` — retourne `AgentResult(reply_text, messages_raw, tool_call_count, tool_error_count)`.
- Publie :
  - le streaming texte/progress sur `relais:messages:streaming:{channel}:{correlation_id}`
  - certains événements de progression sur `relais:messages:outgoing:{channel}`
  - une action `archive` sur `relais:memory:request` avec la réponse complète et `messages_raw` pour archivage Souvenir
  - une trace d'exécution sur `relais:skill:trace` pour Forgeron (fire-and-forget ; uniquement quand `skills_used` non vide) ; `context[CTX_SKILL_TRACE]` contient `skill_names`, `tool_call_count`, `tool_error_count`, `messages_raw`. Publié dans deux cas : (a) après un tour réussi quand `tool_call_count > 0`, (b) sur le chemin DLQ (`AgentExecutionError`) avec `tool_error_count = -1` (sentinelle : tour avorté) et `messages_raw = exc.messages_raw` (conversation partielle capturée depuis le graph state)
  - la réponse finale sur `relais:messages:outgoing_pending` (sans `messages_raw`) ; `context["atelier"]["skills_used"]` estampillé si des skills ont été utilisés
  - en cas d'échec agent (`AgentExecutionError`) : une réponse d'erreur synthétisée par `ErrorSynthesizer` (appel LLM léger) publiée sur `relais:messages:outgoing_pending` pour que l'utilisateur reçoive un message empathique au lieu d'un silence
  - les erreurs finales sur `relais:tasks:failed`
- **Note** : l'annotation inline des skills (anciennement `SkillAnnotator` dans Atelier) a été migrée vers Forgeron (S3 — `ChangelogWriter`). Atelier publie les traces sur `relais:skill:trace` ; Forgeron gère le cycle changelog → consolidation de manière autonome.

### Commandant

- Hérite de `BrickBase` ; `stream_specs()` déclare un seul flux : `relais:commands` (`commandant_group`, `ack_mode="always"`).
- `/help` écrit directement sur `relais:messages:outgoing:{channel}`.
- `/clear` écrit une action `clear` sur `relais:memory:request`.

### Souvenir

- Consomme `relais:memory:request` (actions : `archive`, `clear`, `file_write`, `file_read`, `file_list`).
- Action `archive` : publiée par Atelier après chaque tour LLM complété, contient l'enveloppe de réponse + `messages_raw` (historique LangChain sérialisé pour ce tour).
- Archive chaque tour dans `storage/memory.db` via `LongTermStore`.
- `LongTermStore` : une ligne par tour dans `archived_messages` (upsert sur `correlation_id`) avec
  `messages_raw` JSON, `user_content` et `assistant_content` comme champs dénormalisés.
- L'action `clear` efface les lignes SQLite pour la session et supprime le thread du checkpointer LangGraph (`user_id`).
- Les actions de fichiers (`file_*`) servent les requêtes d'agents via `SouvenirBackend`, répondent sur `relais:memory:response`.

### Archiviste

- Observe `relais:logs`, `relais:events:system`, `relais:events:messages`.
- Observe aussi un sous-ensemble explicite du pipeline, pas tous les streams.
- Écrit `logs/events.jsonl` et relaie certains logs vers le sous-système Python logging.

### Forgeron

Forgeron est le brick d'auto-amélioration des skills. Il dispose de deux pipelines indépendants :

#### Pipeline changelog + consolidation (S3) — Amélioration progressive des skills

- Consomme `relais:skill:trace` (groupe `forgeron_group`, `ack_mode="always"` — les traces sont advisory).
- Atelier publie sur ce stream après chaque tour agent : noms de skills utilisés, nombre d'appels d'outils et d'erreurs, messages bruts LangChain sérialisés (`CTX_SKILL_TRACE`).
- Forgeron accumule une ligne par trace par skill dans SQLite (`SkillTraceStore`).

**Phase 1 — Changelog (chaque trigger, LLM fast)** :
- `ChangelogWriter` (profil `annotation_profile`, LLM rapide) extrait 1-3 observations concrètes et les écrit dans un `CHANGELOG.md` séparé du SKILL.md.
- Déclenché par quatre conditions (dès qu'au moins une est vraie) : erreurs d'outils (`tool_error_count >= annotation_min_tool_errors`), tours avortés (`tool_error_count == -1`, sentinelle DLQ), **success after failure** (le tour courant a 0 erreurs mais le tour précédent du même skill en avait — c'est le "tour de correction" où l'agent a trouvé la bonne approche), ou seuil d'appels cumulés (`annotation_call_threshold`, défaut 5).
- Rate-limité par cooldown Redis `relais:skill:annotation_cooldown:{skill_name}` (TTL `annotation_cooldown_seconds`).
- Le SKILL.md n'est **jamais touché** en Phase 1.

**Phase 2 — Consolidation (périodique, LLM precise)** :
- Déclenchée juste après une écriture Phase 1 si le CHANGELOG.md dépasse `consolidation_line_threshold` lignes (défaut 80) et que le cooldown `relais:skill:consolidation_cooldown:{skill_name}` est expiré.
- `SkillConsolidator` (profil `consolidation_profile`, LLM precise) relit SKILL.md + CHANGELOG.md, réécrit le SKILL.md en absorbant les observations, produit un `CHANGELOG_DIGEST.md` (audit trail), et vide le changelog.
- Toutes les écritures sont atomiques (`.tmp` + `Path.replace()`).
- Le cooldown de consolidation est posé après succès (`consolidation_cooldown_seconds`, défaut 30 min).
- Si `notify_user_on_consolidation` est activé, une notification est publiée sur `relais:messages:outgoing_pending`.

**Fichiers par skill** :
| Fichier | Rôle |
|---------|------|
| `SKILL.md` | Instructions du skill — réécrit uniquement lors de la consolidation |
| `CHANGELOG.md` | Mémoire de travail — observations accumulées entre deux consolidations |
| `CHANGELOG_DIGEST.md` | Audit trail — résumé de chaque consolidation passée |

#### Pipeline auto-création — Création automatique de skills depuis les archives de sessions

- Consomme `relais:memory:request` (groupe `forgeron_archive_group`, indépendant du groupe `souvenir_group` — fan-out complet via deux consumer groups sur le même stream).
- Pour chaque action `archive`, Forgeron extrait les messages utilisateur depuis `CTX_SOUVENIR_REQUEST["messages_raw"]` et appelle `IntentLabeler` (profil Haiku — léger) pour obtenir un label normalisé (ex. `"send_email"`).
- `SessionStore` accumule les sessions labellisées dans SQLite (`session_summaries`) et tient un compteur par label dans `skill_proposals`.
- Quand `min_sessions_for_creation` sessions partagent le même label (et qu'aucun cooldown Redis `relais:skill:creation_cooldown:{label}` n'est actif), `SkillCreator` génère un SKILL.md complet via LLM (profil `precise`) et l'écrit dans `skills_dir/{skill_name}/SKILL.md`.
- La création est idempotente : si le fichier existe déjà, `SkillCreator` retourne `None` sans écraser.
- L'événement `skill.created` (`ACTION_SKILL_CREATED`) est publié sur `relais:events:system` avec `context["forgeron"]` contenant `skill_created`, `skill_path`, `intent_label`, `contributing_sessions`.
- Si `notify_user_on_creation` est activé, une notification est publiée sur `relais:messages:outgoing_pending` pour informer l'utilisateur de la création du skill.

**Fichiers SQLite** (dans `~/.relais/storage/forgeron.db`) :

| Table | Contenu |
|-------|---------|
| `skill_traces` | Traces d'exécution par skill (changelog pipeline) |
| `session_summaries` | Sessions archivées avec leur label d'intention (auto-création pipeline) |
| `skill_proposals` | Propositions de skills agrégées par label (auto-création pipeline) |

---

## Configuration réellement utilisée

### Cascade

La résolution suit :

1. `RELAIS_HOME`
2. `/opt/relais`
3. `./`

### Fichiers principaux

| Fichier | Utilisation réelle |
|--------|---------------------|
| `config/config.yaml` | lit surtout `llm.default_profile` |
| `config/portail.yaml` | utilisateurs, rôles, `unknown_user_policy`, `guest_role` |
| `config/sentinelle.yaml` | ACL et groupes |
| `config/atelier.yaml` | configuration des progress events |
| `config/atelier/profiles.yaml` | profils LLM |
| `config/atelier/mcp_servers.yaml` | serveurs MCP |
| `config/aiguilleur.yaml` | canaux Aiguilleur ; copié par `initialize_user_dir()` ; fallback Discord-only si supprimé manuellement |
| `config/forgeron.yaml` | profils LLM (`annotation_profile`, `consolidation_profile`), seuils (`consolidation_line_threshold`, `annotation_call_threshold`), cooldowns, `skills_dir`, `creation_mode` |

`initialize_user_dir()` copie désormais l'ensemble des templates déclarés dans `common/init.DEFAULT_FILES`, y compris `config/aiguilleur.yaml` et `config/atelier/subagents/relais-config/subagent.yaml`.

### Rechargement à chaud de la configuration

Toutes les briques supportent le rechargement à chaud de leur configuration sans redémarrage :

**Mécanisme de base** (implémenté dans `BrickBase`, hérité par toutes les briques) :
- `_config_watch_paths()` — retourne la liste des fichiers YAML à surveiller
- `_start_file_watcher()` — crée une tâche asyncio via `watch_and_reload()` pour détecter les changements fichier système
- `reload_config()` — recharge et valide la configuration (retourne True/False)
- `_config_reload_listener()` — souscrit au canal Pub/Sub `relais:config:reload:{brick}` pour les déclenchements externes (operator)

**Fichiers surveillés par brique** :
- **Portail**: `config/portail.yaml` (utilisateurs, rôles, politiques)
- **Sentinelle**: `config/sentinelle.yaml` (ACL, groupes)
- **Atelier**: `config/atelier.yaml`, `config/atelier/profiles.yaml`, `config/atelier/mcp_servers.yaml`
- **Souvenir**: aucun fichier surveillé — pas de config rechargeable (Souvenir ne fait pas d'appels LLM)
- **Forgeron**: `config/forgeron.yaml` (seuils, profils LLM, `skills_dir`, `annotation_call_threshold`, `consolidation_line_threshold`)
- **Aiguilleur**: `config/aiguilleur.yaml` (définitions canaux) — voir ci-dessous pour la distinction champs souples/durs

**Flux de rechargement** :
1. Surveillance fichier système via `watchfiles` (inotify sur Linux, FSEvents sur macOS, ReadDirectoryChangesW sur Windows)
2. Changement détecté → appel atomique `safe_reload()` qui : parse le nouveau YAML → acquiert `_config_lock` → swap en place
3. Validation YAML échouée → configuration précédente préservée (fallback sûr)
4. Déclenchement externe : opérateur envoie `"reload"` sur `relais:config:reload:{brick}` (Pub/Sub) → déclenchement manuel sans changement fichier

**Garde fail-closed** (Portail et Sentinelle) :
Une fois qu'une configuration valide et non-permissive a été chargée (`_config_loaded_once = True`), tout rechargement qui aboutirait à un `UserRegistry` vide (Portail) ou un `ACLManager` vide (Sentinelle) est rejeté — la configuration précédente est conservée. Cela empêche une escalade de privilèges par suppression ou vidage du fichier de configuration en production.

**Sauvegarde des configurations** :
- À chaque rechargement réussi, la configuration précédente est archivée dans `~/.relais/config/backups/{brick}_{timestamp}.yaml`
- Rétention : max 5 versions par brique
- Permet audit et rollback manuel si nécessaire

**Hot-reload Aiguilleur — champs souples vs durs** :
Le rechargement de `aiguilleur.yaml` par l'Aiguilleur distingue deux catégories de champs :

| Catégorie | Champs | Effet |
|-----------|--------|-------|
| **Souples** | `profile`, `prompt_path`, `streaming` | Mis à jour en direct sans redémarrer l'adaptateur. `profile` est mis à jour via `ProfileRef.update()` (thread-safe) ; les adaptateurs lisent `adapter.config` à chaque message entrant. |
| **Durs** | `type`, `class_path`, `enabled`, `command` | Changement détecté → WARNING loggé. Redémarrage du process requis pour appliquer. L'ajout/suppression de canaux nécessite également un redémarrage. |

Le mécanisme repose sur un thread daemon `aiguilleur-config-watcher` qui surveille le fichier via `watchfiles` et appelle `_reload_channel_profiles()` à chaque changement. Le thread est arrêté proprement par `_shutdown_event` lors du SIGTERM.

**Cas d'usage** :
- Modification des ACL (Sentinelle) sans redémarrage
- Ajout/suppression de profils LLM (Atelier) en direct
- Changement de politique utilisateur (Portail)
- Changement de profil LLM ou de chemin d'overlay de prompt (Aiguilleur) en direct, sans redémarrer l'adaptateur Discord/Telegram

---

## Prompts

`assemble_system_prompt()` assemble actuellement 4 couches. Tous les chemins sont explicites — aucun chemin n'est inféré par convention à partir du nom de rôle ou du canal :

1. `prompts/soul/SOUL.md` — personnalité de base (toujours chargée)
2. `role_prompt_path` — chemin relatif configuré dans `portail.yaml` (`roles[*].prompt_path`), estampillé dans `UserRecord.role_prompt_path` par Portail
3. `user_prompt_path` — chemin relatif configuré dans `portail.yaml` (`users[*].prompt_path`), estampillé dans `UserRecord.prompt_path` par Portail. Indépendant de `role_prompt_path` — aucun fallback entre les deux.
4. `channel_prompt_path` — chemin relatif configuré dans `aiguilleur.yaml` (`channels[*].prompt_path`), estampillé dans `context.aiguilleur["channel_prompt_path"]` par l'Aiguilleur

Les fichiers `prompts/policies/*.md` existent dans les templates, mais ils ne sont pas injectés automatiquement dans le prompt principal par le code actuel.

---

## Envelope — contrat `action`

Depuis 2026-04-10, `Envelope.to_json()` **lève `ValueError`** si `envelope.action` est vide. Chaque site producteur doit positionner explicitement `action` avant publication :

- les réponses dérivées via `Envelope.create_response_to()` ou `Envelope.from_parent()` ne conservent pas l'action source — le code appelant doit affecter l'action cible (`ACTION_MESSAGE_OUTGOING_PENDING`, `ACTION_MESSAGE_OUTGOING`, etc.) avant d'appeler `xadd`.
- Sites mis à jour lors de l'introduction de cette contrainte : `atelier/main.py` (réponse finale), `sentinelle/main.py` (rejection inline), `commandant/commands.py` (`/help`), `souvenir/handlers/clear_handler.py` (confirmation `/clear`).
- Les fixtures de tests construisent désormais les enveloppes avec `action=` explicite.

Cette contrainte évite qu'une enveloppe sans intent déclaré ne traverse la pipeline.

---

## Stockage

### Redis

- transport principal du pipeline
- socket local par défaut : `<RELAIS_HOME>/redis.sock`
- port TCP `127.0.0.1:6379` ouvert en plus du socket Unix pour les services externes (typiquement la passerelle `baileys-api`)

### SQLite

- fichier principal : `<RELAIS_HOME>/storage/memory.db`
- utilisé par `LongTermStore` et `FileStore`
- initialisation recommandée : `alembic upgrade head`

Il n'existe pas de `audit.db` prise en charge par l'Archiviste dans l'implémentation actuelle.

---

## Démarrage

### Supervisé

Le chemin recommandé est :

```bash
./supervisor.sh start all              # Démarrer le système
./supervisor.sh --verbose start all    # Démarrer + suivre les logs en temps réel
```

Cela démarre Redis local puis les briques Python via `launcher.py`. Le flag `--verbose` affiche les logs de toutes les briques après le démarrage (Ctrl+C pour détacher sans arrêter supervisord).

Groupes supervisord :

| Groupe | Contenu | Auto-start `supervisor.sh start all` |
|--------|---------|--------------------------------------|
| `infra` | `courier` (Redis) | oui |
| `core` | `portail`, `sentinelle`, `atelier`, `souvenir`, `commandant`, `archiviste`, `forgeron` | oui |
| `relays` | `aiguilleur` | oui |
| `optional` | `baileys-api` (passerelle Node.js WhatsApp, lancée via `scripts/run_baileys.py`) | **non** — démarré à la demande par le sous-agent `relais-config` ou manuellement via `supervisorctl start baileys-api` |

### Manuel

```bash
redis-server config/redis.conf
uv run python portail/main.py
uv run python sentinelle/main.py
uv run python atelier/main.py
uv run python souvenir/main.py
uv run python forgeron/main.py
uv run python commandant/main.py
uv run python archiviste/main.py
uv run python aiguilleur/main.py
```

---

## Références utiles

- [README.md](/Users/benjaminmarchand/IdeaProjects/relais/README.md)
- [docs/ENV.md](/Users/benjaminmarchand/IdeaProjects/relais/docs/ENV.md)
- [tests/test_smoke_e2e.py](/Users/benjaminmarchand/IdeaProjects/relais/tests/test_smoke_e2e.py)
