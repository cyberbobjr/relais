# RELAIS — Architecture Technique

**Dernière mise à jour :** 2026-04-04

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
| `relais:messages:outgoing:{channel}` | Sentinelle, Atelier, Commandant, Souvenir | Aiguilleur, Souvenir |

### Mémoire

| Stream / clé | Producteur | Consommateur |
|--------------|------------|--------------|
| `relais:memory:request` | Commandant | Souvenir |

### Streaming et erreurs

| Stream | Producteur | Consommateur |
|--------|------------|--------------|
| `relais:messages:streaming:{channel}:{correlation_id}` | Atelier | adaptateur de canal streaming |
| `relais:tasks:failed` | Atelier | observation / diagnostic |
| `relais:admin:pending_users` | Portail | revue manuelle |
| `relais:logs` | toutes les briques | Archiviste |
| `relais:events:system` | divers | Archiviste |
| `relais:events:messages` | divers | Archiviste |

---

## Comportement par brique

### Aiguilleur

- Charge les canaux via `load_channels_config()`.
- Démarre un adaptateur par canal activé.
- L'implémentation complète présente dans le dépôt est surtout l'adaptateur Discord.
- Côté Discord, l'entrée est `relais:messages:incoming` et la sortie `relais:messages:outgoing:discord`.
- Chaque adaptateur estampille `envelope.metadata["channel_profile"]` depuis `ChannelConfig.profile` (channels.yaml).
- Chaque adaptateur estampille `envelope.metadata["channel_prompt_path"]` depuis `ChannelConfig.prompt_path` (channels.yaml). `None` si non configuré — aucun overlay de canal n'est chargé.

### Portail

- Consomme `relais:messages:incoming`.
- Valide l'enveloppe.
- Résout l'utilisateur avec `UserRegistry`.
- Écrit `metadata["user_record"]`, `metadata["user_id"]` et `metadata["llm_profile"]` (depuis `channel_profile` ou `"default"`).
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
- Exécute `AgentExecutor`.
- Publie :
  - le streaming texte/progress sur `relais:messages:streaming:{channel}:{correlation_id}`
  - certains événements de progression sur `relais:messages:outgoing:{channel}`
  - la réponse finale (avec `metadata["messages_raw"]`) sur `relais:messages:outgoing_pending`
  - les erreurs finales sur `relais:tasks:failed`

### Commandant

- Consomme `relais:commands`.
- `/help` écrit directement sur `relais:messages:outgoing:{channel}`.
- `/clear` écrit une action `clear` sur `relais:memory:request`.

### Souvenir

- Consomme `relais:memory:request` (actions : `clear`, `file_write`, `file_read`, `file_list`).
- Observe les streams `relais:messages:outgoing:{channel}` pour les canaux de `_DEFAULT_CHANNELS`.
- Archive chaque tour dans `storage/memory.db` via `LongTermStore`.
- `LongTermStore` : une ligne par tour dans `archived_messages` (upsert sur `correlation_id`) avec
  `messages_raw` JSON, `user_content` et `assistant_content` comme champs dénormalisés.
- L'action `clear` efface les lignes SQLite pour la session et supprime le thread du checkpointer LangGraph (`user_id`).

### Archiviste

- Observe `relais:logs`, `relais:events:system`, `relais:events:messages`.
- Observe aussi un sous-ensemble explicite du pipeline, pas tous les streams.
- Écrit `logs/events.jsonl` et relaie certains logs vers le sous-système Python logging.

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
| `config/channels.yaml` | canaux Aiguilleur si fichier présent ; sinon fallback Discord |

`initialize_user_dir()` ne copie pas `channels.yaml` actuellement.

---

## Prompts

`assemble_system_prompt()` assemble actuellement 4 couches. Tous les chemins sont explicites — aucun chemin n'est inféré par convention à partir du nom de rôle ou du canal :

1. `prompts/soul/SOUL.md` — personnalité de base (toujours chargée)
2. `role_prompt_path` — chemin relatif configuré dans `portail.yaml` (`roles[*].prompt_path`), estampillé dans `UserRecord.role_prompt_path` par Portail
3. `user_prompt_path` — chemin relatif configuré dans `portail.yaml` (`users[*].prompt_path`), estampillé dans `UserRecord.prompt_path` par Portail. Indépendant de `role_prompt_path` — aucun fallback entre les deux.
4. `channel_prompt_path` — chemin relatif configuré dans `channels.yaml` (`channels[*].prompt_path`), estampillé dans `envelope.metadata["channel_prompt_path"]` par l'Aiguilleur

Les fichiers `prompts/policies/*.md` existent dans les templates, mais ils ne sont pas injectés automatiquement dans le prompt principal par le code actuel.

---

## Stockage

### Redis

- transport principal du pipeline
- socket local par défaut : `<RELAIS_HOME>/redis.sock`

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
./supervisor.sh start all
```

Cela démarre Redis local puis les briques Python via `launcher.py`.

### Manuel

```bash
redis-server config/redis.conf
uv run python portail/main.py
uv run python sentinelle/main.py
uv run python atelier/main.py
uv run python souvenir/main.py
uv run python commandant/main.py
uv run python archiviste/main.py
uv run python aiguilleur/main.py
```

---

## Références utiles

- [README.md](/Users/benjaminmarchand/IdeaProjects/relais/README.md)
- [docs/ENV.md](/Users/benjaminmarchand/IdeaProjects/relais/docs/ENV.md)
- [tests/test_smoke_e2e.py](/Users/benjaminmarchand/IdeaProjects/relais/tests/test_smoke_e2e.py)
