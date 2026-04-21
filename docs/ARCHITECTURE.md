# RELAIS — Architecture Technique

**Dernière mise à jour :** 2026-04-19

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
| `relais:messages:incoming:horloger` | Horloger | Portail |
| `relais:security` | Portail | Sentinelle |
| `relais:tasks` | Sentinelle | Atelier |
| `relais:commands` | Sentinelle | Commandant |
| `relais:messages:outgoing_pending` | Atelier | Sentinelle |
| `relais:messages:outgoing:{channel}` | Sentinelle, Atelier, Commandant | Aiguilleur |

### Mémoire

| Stream / clé | Producteur | Consommateur |
|--------------|------------|--------------|
| `relais:memory:request` | Atelier, Commandant, Forgeron | Souvenir (`souvenir_group`), Forgeron (`forgeron_archive_group`) |
| `relais:memory:response` | Souvenir | agents (via SouvenirBackend) |
| `relais:memory:response:{correlation_id}` (Redis List) | Souvenir (HistoryReadHandler) | Forgeron (BRPOP synchrone) |

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
| `relais:whatsapp:pairing` (Redis String JSON, TTL 300s) | `whatsapp_configure` tool / `python -m aiguilleur.channels.whatsapp configure --action pair` | Adaptateur WhatsApp / opérateur | Contexte de pairing QR actif (`KEY_WHATSAPP_PAIRING`) |

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
  - **WhatsApp** (`aiguilleur/channels/whatsapp/adapter.py`) — serveur webhook aiohttp écoutant la passerelle externe [fazer-ai/baileys-api](https://github.com/fazer-ai/baileys-api) (Node.js, lancée par `scripts/run_baileys.py` sous supervisord, programme `baileys-api` dans le groupe `optional`). L'adaptateur transcrit les events WhatsApp entrants en Envelope → `relais:messages:incoming`, et envoie les réponses sortantes via l'API REST de la passerelle après conversion Markdown→WhatsApp (`common/markdown_converter.convert_md_to_whatsapp()` pour `*bold*`, `_italic_`, `~strike~` natifs). Installation, configuration et pairing via `python -m aiguilleur.channels.whatsapp` CLI ou via les tools LangChain `whatsapp_install`, `whatsapp_configure`, `whatsapp_uninstall`.
  - **REST** (`aiguilleur/channels/rest/adapter.py`) — adaptateur HTTP/JSON et SSE pour les clients programmatiques (CLI, CI, TUI). Expose:
    - `POST /v1/messages` — Envoyer un message et recevoir la réponse LLM (JSON ou SSE streaming)
    - `GET /v1/history?session_id=...&limit=...` — Récupérer l'historique d'une session (ownership enforcement via user_id)
    - `GET /v1/events` — Persistent SSE push stream: fan-out outgoing messages to concurrent subscribers (same user ID, different clients). Powered by `PushRegistry` (per-user XREAD reader tasks) and `relais:messages:outgoing:rest:{user_id}` per-user streams.
    - `GET /docs/sse` — Playground SSE interactif
    
    Authentification Bearer via clés API dans `portail.yaml`. Les clés API sont résolues via `UserRegistry.resolve_rest_api_key()` (hachage HMAC-SHA256, jamais stockées en clair). L'adaptateur filtre les `ACTION_MESSAGE_PROGRESS` entrants pour ne résoudre que la réponse finale. L'adaptateur mirroise aussi les enveloppes sortantes vers les streams `relais:messages:outgoing:rest:{user_id}` de sorte que les clients SSE abonnés puissent les lire indépendamment.
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
- Gère l'historique conversationnel via un checkpointer LangGraph persistant (`AsyncSqliteSaver`, `checkpoints.db`). L'ID de thread est `f"{user_id}:{session_id}"` (isolation par session pour Phase 4b).
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
- **Modules extraits** :
  - `atelier/streaming.py` — `StreamBuffer`, `_extract_thinking`, `_has_tool_use_block` ; helpers de buffering de tokens extraits de `agent_executor.py` pour maintenir chaque module sous 800 lignes.
  - `atelier/subagents_resolver.py` — fonctions pures de résolution des tokens `tools:` et `skills:` (`_load_tools_from_import`, `_resolve_skill_token`, …) ; extraites de `atelier/subagents.py`.
  - `atelier/display_config.py` — `DisplayConfig` (dataclass frozen) chargée depuis `atelier.yaml` (section `display:`), remplace l'ancien `progress_config.py`.
- **Note** : l'annotation inline des skills (anciennement `SkillAnnotator` dans Atelier) a été migrée vers Forgeron (S3 — `ChangelogWriter`). Atelier publie les traces sur `relais:skill:trace` ; Forgeron gère le cycle changelog → consolidation de manière autonome.

### Commandant

- Hérite de `BrickBase` ; `stream_specs()` déclare un seul flux : `relais:commands` (`commandant_group`, `ack_mode="always"`).
- `/help` écrit directement sur `relais:messages:outgoing:{channel}`.
- `/clear` écrit une action `clear` sur `relais:memory:request`.
- `/sessions` écrit une action `sessions` sur `relais:memory:request` pour lister les sessions récentes de l'utilisateur.
- `/resume <session_id>` écrit une action `resume` sur `relais:memory:request` pour reprendre une session précédente. Valide que la session_id appartient à l'utilisateur (ownership enforcement).

### Souvenir

- Consomme `relais:memory:request` (actions : `archive`, `clear`, `file_write`, `file_read`, `file_list`, `sessions`, `resume`, `history_read`).
- Action `archive` : publiée par Atelier après chaque tour LLM complété, contient l'enveloppe de réponse + `messages_raw` (historique LangChain sérialisé pour ce tour).
- Archive chaque tour dans `storage/memory.db` via `LongTermStore`.
- `LongTermStore` : une ligne par tour dans `archived_messages` (upsert sur `correlation_id`) avec
  `messages_raw` JSON, `user_content` et `assistant_content` comme champs dénormalisés.
- L'action `clear` efface les lignes SQLite pour la session et supprime le thread du checkpointer LangGraph (thread_id `user_id:session_id`).
- Action `sessions` : retourne une liste formatée des sessions récentes de l'utilisateur (avec ownership enforcement via `user_id`), publie la réponse sur `relais:messages:outgoing:{channel}` via `SessionsHandler`.
- Action `resume` : récupère l'historique complet d'une session précédente (ownership enforcement via `user_id`), publie la réponse sur `relais:messages:outgoing:{channel}` via `ResumeHandler`.
- Action `history_read` : publiée par Forgeron pour lire l'historique complet des messages bruts d'une session ; le handler lit de SQLite, tronque selon un budget de tokens (~4 chars/token), et publie le résultat JSON sur `relais:memory:response:{correlation_id}` (Redis List avec TTL 60s) pour que Forgeron le récupère via `BRPOP` (handshake synchrone).
- Les actions de fichiers (`file_*`) servent les requêtes d'agents via `SouvenirBackend`, répondent sur `relais:memory:response`.
- Handlers: `ArchiveHandler`, `ClearHandler`, `FileWriteHandler`, `FileReadHandler`, `FileListHandler`, `SessionsHandler`, `ResumeHandler`, `HistoryReadHandler` — pas d'appels LLM dans Souvenir.

### Horloger

- **Producteur uniquement** : `stream_specs()` retourne `[]` ; BrickBase attend sur `shutdown_event` pendant que la `_tick_loop` tourne en tâche de fond.
- Lit les specs de jobs YAML dans `$RELAIS_HOME/config/horloger/jobs/*.yaml` (un fichier par job) ; rechargement automatique via watchfiles.
- À chaque tick (`tick_interval_seconds`, défaut 30s) :
  1. `JobRegistry.reload()` + `Scheduler.sync_jobs()` — rechargement des jobs et purge de l'historique des jobs supprimés.
  2. `Scheduler.get_due_jobs()` — classifie les jobs en `to_trigger` / `to_skip` selon quatre gardes : guard futur, guard catch-up, guard désactivé, guard double-fire.
  3. Publie une envelope de déclenchement sur `relais:messages:incoming:horloger` pour chaque job à déclencher.
  4. Enregistre chaque outcome dans `storage/horloger.db` (SQLite via SQLModel + aiosqlite) : statuts `triggered`, `publish_failed`, `skipped_catchup`, `skipped_disabled`, `skipped_double_fire`.
- **Patron canal virtuel** : l'envelope traverse le pipeline complet (Portail → Sentinelle → Atelier) comme un vrai message utilisateur.
  - `sender_id = f"horloger:{job.owner_id}"` pour que la Sentinelle applique le bon ACL.
  - `context["portail"]` pré-estampillé (`user_id`, `llm_profile`) pour éviter la lookup UserRegistry (`"horloger"` n'est pas un canal réel dans `portail.yaml`).
  - `context["aiguilleur"]["reply_to"] = job.channel` pour que Sentinelle route la réponse vers le bon canal de sortie.
- Guard anti-tempête : les jobs dont le dernier temps planifié est antérieur à `catch_up_window_seconds` (défaut 120s) sont ignorés, non re-déclenchés, après un redémarrage.
- **Sous-agent `horloger-manager`** (natif) : gère le CRUD des fichiers YAML de jobs via les commandes `/horloger` ou `/schedule`.

| Stream | Direction |
|--------|-----------|
| `relais:messages:incoming:horloger` | Produit par Horloger, consommé par `portail_group` |
| `relais:logs` | Produit par Horloger (BrickBase) |

### Archiviste

- Observe `relais:logs`, `relais:events:system`, `relais:events:messages`.
- Observe aussi un sous-ensemble explicite du pipeline, pas tous les streams.
- Écrit `logs/events.jsonl` et relaie certains logs vers le sous-système Python logging.

### Forgeron

Forgeron est le brick d'auto-amélioration des skills. Il dispose de deux pipelines indépendants :

#### Pipeline édition directe — Amélioration progressive des skills

- Consomme `relais:skill:trace` (groupe `forgeron_group`, `ack_mode="always"` — les traces sont advisory).
- Atelier publie sur ce stream après chaque tour agent : noms de skills utilisés, nombre d'appels d'outils et d'erreurs, messages bruts LangChain sérialisés (`CTX_SKILL_TRACE`), et `skill_paths` (dict `{skill_name: chemin_absolu}` pour les skills bundle).
- Forgeron accumule une ligne par trace par skill dans SQLite (`SkillTraceStore`).

**Édition directe (`SkillEditor`, LLM precise)** :
- `SkillEditor` reçoit le SKILL.md courant + la trace de conversation scopée au skill cible (via `scope_messages_to_skill`). Il appelle le LLM une seule fois avec `with_structured_output` pour produire un SKILL.md réécrit et un flag `changed`.
- Le SKILL.md est écrit uniquement si `changed=True` et que le contenu diffère du fichier existant.
- Déclenché par quatre conditions (dès qu'au moins une est vraie) : erreurs d'outils (`tool_error_count >= edit_min_tool_errors`), tours avortés (`tool_error_count == -1`, sentinelle DLQ), **success after failure** (le tour courant a 0 erreurs mais le tour précédent du même skill en avait — c'est le "tour de correction" où l'agent a trouvé la bonne approche), ou seuil d'appels cumulés (`edit_call_threshold`, défaut 10).
- Rate-limité par cooldown Redis `relais:skill:edit_cooldown:{skill_name}` (TTL `edit_cooldown_seconds`).
- Pour les skills provenant d'un bundle, `skill_paths` indique le chemin absolu du répertoire ; `SkillEditor` utilise ce chemin en priorité sur la résolution standard.

**Profil LLM** : `edit_profile` (défaut `"precise"`) — un seul appel LLM par trigger (ni phase rapide ni consolidation périodique).

#### Pipeline auto-création — Création automatique de skills depuis les archives de sessions

- Consomme `relais:memory:request` (groupe `forgeron_archive_group`, indépendant du groupe `souvenir_group` — fan-out complet via deux consumer groups sur le même stream).
- Pour chaque action `archive`, Forgeron extrait les messages utilisateur depuis `CTX_SOUVENIR_REQUEST["messages_raw"]` et appelle `IntentLabeler` (profil Haiku — léger) pour obtenir un label normalisé (ex. `"send_email"`).
- `SessionStore` accumule les sessions labellisées dans SQLite (`session_summaries`) et tient un compteur par label dans `skill_proposals`.
- Quand `min_sessions_for_creation` sessions partagent le même label (et qu'aucun cooldown Redis `relais:skill:creation_cooldown:{label}` n'est actif), `SkillCreator` génère un SKILL.md complet via LLM (profil `precise`) et l'écrit dans `skills_dir/{skill_name}/SKILL.md`.
- La création est idempotente : si le fichier existe déjà, `SkillCreator` retourne `None` sans écraser.
- L'événement `skill.created` (`ACTION_SKILL_CREATED`) est publié sur `relais:events:system` avec `context["forgeron"]` contenant `skill_created`, `skill_path`, `intent_label`, `contributing_sessions`.
- Si `notify_user_on_creation` est activé, une notification est publiée sur `relais:messages:outgoing_pending` pour informer l'utilisateur de la création du skill.

#### Pipeline correction — Redesign de skills via analyse des traces

- Déclenché par `IntentLabeler` qui détecte une correction dans un pattern de session (champ `is_correction` de `IntentLabelResult`).
- `_trigger_skill_design()` orchestre un handshake synchrone :
  1. Publie une requête `history_read` sur `relais:memory:request` pour que Souvenir serve l'historique complet.
  2. Envoie une notification utilisateur sur `relais:messages:outgoing_pending` (avant le BRPOP pour éviter tout blocage).
  3. Attend la réponse via `BRPOP` sur `relais:memory:response:{correlation_id}` (timeout configurable, défaut quelques secondes).
  4. Si l'historique arrive, publie un `ACTION_MESSAGE_TASK` sur `relais:tasks` avec `force_subagent="skill-designer"` et les données de correction dans `context["forgeron"]` (`corrected_behavior`, `history_turns`, `skill_name_hint` optionnel).
- Le sous-agent `skill-designer` (natif Atelier) reçoit ces données et génère un SKILL.md révisé via l'outil `WriteSkillTool`.

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

1. `RELAIS_HOME` (par défaut `~/.relais`)

### Fichiers principaux

| Fichier | Utilisation réelle |
|--------|---------------------|
| `config/config.yaml` | lit surtout `llm.default_profile` |
| `config/portail.yaml` | utilisateurs, rôles, `unknown_user_policy`, `guest_role` |
| `config/sentinelle.yaml` | ACL et groupes |
| `config/atelier.yaml` | configuration des display events (section `display:`) |
| `config/atelier/profiles.yaml` | profils LLM |
| `config/atelier/mcp_servers.yaml` | serveurs MCP |
| `config/aiguilleur.yaml` | canaux Aiguilleur ; copié par `initialize_user_dir()` ; fallback Discord-only si supprimé manuellement |
| `config/forgeron.yaml` | profils LLM (`edit_profile`, `llm_profile`), seuils (`edit_call_threshold`, `edit_min_tool_errors`), cooldowns (`edit_cooldown_seconds`), `skills_dir`, `edit_mode`, `creation_mode`, `correction_mode` |
| `config/horloger.yaml` | `tick_interval_seconds`, `catch_up_window_seconds`, `jobs_dir`, `db_path` |

`initialize_user_dir()` copie l'ensemble des templates déclarés dans `common/init.DEFAULT_FILES`, y compris `config/aiguilleur.yaml` et `config/tui/config.yaml` (configuration du client TUI). Les sous-agents natifs (`relais-config`, …) sont **exclus** de cette copie : ils sont livrés directement dans `atelier/subagents/` (arbre source) et chargés en 2e tier par `SubagentRegistry`. Seul le répertoire `config/atelier/subagents/` est créé vide dans `RELAIS_HOME` pour accueillir les sous-agents custom de l'opérateur.

### Architecture 2-tier des sous-agents (Subagents)

L'Atelier dispose d'une architecture 2-tier pour les sous-agents :

| Tier | Localisation | Priorité | Initialisation | Modification |
|------|-------------|----------|---|---|
| **User** | `$RELAIS_HOME/config/atelier/subagents/{name}/` | 1ère (prioritaire) | Créés manuellement par l'opérateur | Modifiables sans redémarrage (hot-reload) |
| **Native** | `atelier/subagents/{name}/` (source) | 2e (fallback) | Livrés avec le dépôt | Modifiables via le code source ; hot-reload supporté |

**Chargement** (`SubagentRegistry.load()`):
1. Scanne d'abord `$RELAIS_HOME/config/atelier/subagents/`
2. Puis scanne `atelier/subagents/` (native)
3. Premier match par nom gagne (user overrides native)
4. Chaque répertoire doit contenir `subagent.yaml`

**Subagents natifs livrés** :
- `relais-config` (`atelier/subagents/relais-config/`) — configuration CRUD, outils WhatsApp, etc.
- `horloger-manager` (`atelier/subagents/horloger-manager/`) — CRUD des fichiers YAML de jobs Horloger ; accessible via `/horloger` ou `/schedule`.

**Utilisation** :
- L'accès par rôle est contrôlé via `allowed_subagents` dans `portail.yaml` (fnmatch patterns, e.g. `["relais-config"]`, `["my-*"]`)
- Aucun changement de code requis pour ajouter/modifier des subagents — Atelier les découvre automatiquement

**Hot-reload** :
- Atelier surveille `$RELAIS_HOME/config/atelier/subagents/` et `atelier/subagents/` via `watchfiles`
- Un changement dans l'un ou l'autre déclenche un rechargement atomique du registre
- Les subagents en cours d'exécution ne sont pas interrompus

**Validation des tokens d'outils et état dégradé** :
- À l'appel de `load()`, les tokens `module:<dotted.path>` et les références statiques `<bare-name>` sont validés au démarrage (les formes `mcp:`, `inherit` et `local:` sont dynamiques et sautées à ce stade)
- Un subagent est considéré comme **dégradé** si au moins un de ses tokens d'outils n'a pas pu être résolu :
  - **Au démarrage** (validation statique) : le token invalide est enregistré dans le champ `degraded_tokens` du `SubagentSpec`
  - **À l'exécution** (résolution runtime) : le token échoue lors du traitement d'une requête et est ajouté à `_runtime_degraded` du registre
- La propriété `degraded_names` retourne l'ensemble des noms de subagents dégradés (startup + runtime)
- Chaque subagent dégradé est loggé avec un WARNING indiquant le token problématique et la raison (module non importable, outil statique non trouvé, etc.)
- Les subagents dégradés ne sont pas exclus du pipeline — ils restent accessibles, mais exécutent uniquement avec les outils valides (fail-closed, jamais fail-silent)

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
- **Atelier**: `config/atelier.yaml`, `config/atelier/profiles.yaml`, `config/atelier/mcp_servers.yaml`, `config/atelier/subagents/` (user), `atelier/subagents/` (native)
- **Souvenir**: aucun fichier surveillé — pas de config rechargeable (Souvenir ne fait pas d'appels LLM)
- **Forgeron**: `config/forgeron.yaml` (profils LLM, `skills_dir`, `edit_call_threshold`, `edit_mode`, `creation_mode`)
- **Horloger**: aucun fichier surveillé — le `tick_loop` se recharge via `watchfiles` sur `jobs_dir` (un fichier YAML par job)
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

> **Sécurité** : `supervisor.sh stop <name>` et `restart <name>` valident le nom de service via `validate_service_name()` (caractères autorisés : alphanumérique, `_`, `:`, `.`, `-`). Un nom invalide cause une sortie immédiate avec code d'erreur.

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

## Système de bundles (`common/bundles.py`)

Les bundles sont des archives ZIP distribuant subagents, skills et tools en une seule unité installable.

### Structure d'un bundle

```
my-bundle.zip
└── my-bundle/           # dossier racine = nom du bundle
    ├── bundle.yaml      # manifeste obligatoire (name, description, version, author)
    ├── subagents/       # optionnel — un répertoire par subagent
    ├── skills/          # optionnel — un répertoire par skill
    └── tools/           # optionnel — fichiers .py exportant des BaseTool
```

### Installation et découverte

- Destination : `~/.relais/bundles/<bundle-name>/`
- **CLI** : `relais bundle install/uninstall/list`
- **Slash command** : `/bundle install|uninstall|list`
- **TUI** : onglet Bundles (Ctrl+B)

Sécurité : protection ZIP bomb (> 50 Mo rejeté) + protection path traversal.

### Intégration dans Atelier

| Composant | Comportement |
|-----------|--------------|
| `ToolRegistry` | Scanne `~/.relais/bundles/*/tools/*.py` ; tague chaque outil avec `_bundle_name` |
| `SubagentRegistry` | Tier 3 (après config utilisateur et natif) : `~/.relais/bundles/*/subagents/` |
| `ToolPolicy` | Fusionne `~/.relais/bundles/*/skills/*/` dans la résolution des skills |
| Hot-reload | `watchfiles` surveille `~/.relais/bundles/` ; rechargement atomique |

Les subagents de bundles restent soumis au contrôle d'accès `allowed_subagents` de `portail.yaml`.

Voir `docs/BUNDLES.md` pour la spécification complète du format.

---

## Outils clients (`tools/`)

### TUI Python (`tools/tui/`)

Client terminal interactif pour le canal REST. Paquet Python indépendant (`relais-tui`), point d'entrée `relais` (script console défini dans `pyproject.toml`). Implémenté avec **prompt_toolkit** (pas Textual).

- **`relais_tui/app.py`** — application principale `RelaisApp` (prompt_toolkit) : layout HSplit (banner + chat + input), key bindings (Enter=send, Escape+Enter/Ctrl+J=newline, Ctrl+C/D=quit), boucle SSE asynchrone, rendu markdown streamé.
- **`relais_tui/chat_state.py`** — `ChatState` : gestion de l'état de la conversation (messages user/assistant/tool_call/tool_result), append immutable, formatage pour l'affichage.
- **`relais_tui/md_stream.py`** — rendu markdown incrémental token-par-token avec `MdStreamRenderer` ; buffer de tokens, flush sur newline, conversion rich → prompt_toolkit `FormattedText`.
- **`relais_tui/paste_handler.py`** — détection de grande colle (`is_large_paste`), capture d'image depuis le presse-papier (`grab_image_from_clipboard`), résumé de colle texte (`summarize_paste`).
- **`relais_tui/attachments.py`** — dataclasses `ImagePayload` et `PasteBlock` pour les pièces jointes injectées dans le message.
- **`relais_tui/config.py`** — chargement de la configuration depuis `<relais_home>/config/tui/config.yaml` (cascade RELAIS_HOME / `~/.relais`). Historique TUI par défaut dans `~/.relais/storage/tui/history`.
- **`relais_tui/client.py`** — client REST/SSE asynchrone : `POST /v1/messages` puis lecture du stream SSE.
- **`relais_tui/sse_parser.py`** — parseur SSE stateful ; `SSEEvent = TokenEvent | DoneEvent | ProgressEvent | ErrorEvent | Keepalive`.
- **`relais_tui/bundles.py`** — opérations bundle locales (list/install/uninstall) utilisées par l'onglet Bundles.
- **`relais_tui/screens/bundles_screen.py`** — écran Bundles (DataTable + Install/Uninstall).

La configuration TUI est initialisée par `initialize_user_dir()` depuis `config/tui/config.yaml.default`.

### TUI TypeScript (`tools/tui-ts/`)

Client terminal alternatif en TypeScript/Bun. Utilise **@opentui/solid** (rendu terminal SolidJS) comme moteur de rendu et **solid-js** pour la réactivité. Compilable en binaire autonome (`bun build --compile`).

- **`src/main.tsx`** — point d'entrée : charge la config, instancie `RelaisClient`, rend `<App>` avec `@opentui/solid`, hydrate l'historique de session après le premier rendu.
- **`src/app.tsx`** — composant racine `App` : layout (ChatHistory + InputArea + StatusBar), gestion sélection/copie via `useSelectionHandler` et `useKeyHandler`, dispatch des commandes `/clear` via `handleClear`.
- **`src/components/ChatHistory.tsx`** — `<scrollbox>` avec sticky-scroll auto-follow, affiche `<Banner>` + liste de `<MessageBubble>`.
- **`src/components/InputArea.tsx`** — zone de saisie multi-ligne, Enter=submit, Shift+Enter=newline.
- **`src/components/StatusBar.tsx`** — barre de statut (session ID, état envoi, flash copie, bannière d'erreur).
- **`src/components/MessageBubble.tsx`** — bulle de message user/assistant avec rendu markdown.
- **`src/lib/`** — `client.ts` (REST/SSE), `sse-parser.ts` (parseur SSE stateful), `store.ts` (état réactif SolidJS), `config.ts` (YAML config + RELAIS_HOME resolution), `clipboard.ts`, `logger.ts`, `handle-clear.ts` (logique `/clear` : vide l'UI immédiatement puis envoie `/clear` au backend pour purger l'historique Redis+SQLite, réinitialise le `sessionId`, affiche un flash de confirmation ou une bannière d'erreur).

Dépendances : `@opentui/core`, `@opentui/solid`, `solid-js`, `yaml`. Runtime : Bun ≥ 1.3.

---

## Références utiles

- [README.md](/Users/benjaminmarchand/IdeaProjects/relais/README.md)
- [docs/ENV.md](/Users/benjaminmarchand/IdeaProjects/relais/docs/ENV.md)
- [tests/test_smoke_e2e.py](/Users/benjaminmarchand/IdeaProjects/relais/tests/test_smoke_e2e.py)
