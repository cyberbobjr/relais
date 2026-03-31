# Phase 5 — Config & Infrastructure

**Fichiers à modifier:**
- `supervisord.conf`
- `config/redis.conf`

**Complexité:** LOW

---

## 1. `supervisord.conf`

### Ajout du programme `commandant`

Insérer **après le bloc `[program:souvenir]`** (ligne ~70) et avant le commentaire `; priority 20`:

```ini
[program:commandant]
command=/Users/benjaminmarchand/.local/bin/uv run python commandant/main.py
priority=10
autostart=true
autorestart=true
stdout_logfile=./.relais/logs/commandant.log
redirect_stderr=true
environment=PYTHONUNBUFFERED="1",PYTHONPATH="/Users/benjaminmarchand/IdeaProjects/relais",RELAIS_HOME="/Users/benjaminmarchand/IdeaProjects/relais/.relais"
```

### Mise à jour du groupe `[group:core]`

Remplacer :
```ini
[group:core]
programs=portail,sentinelle,atelier,souvenir,archiviste
```

Par :
```ini
[group:core]
programs=portail,sentinelle,atelier,souvenir,archiviste,commandant
```

---

## 2. `config/redis.conf`

### Ajout de l'utilisateur `commandant`

Ajouter après la ligne `user souvenir ...` :

```
user commandant on >pass_commandant ~relais:messages:incoming ~relais:messages:outgoing:* ~relais:memory:request ~relais:state:* ~relais:logs +@all
```

**Permissions détaillées :**
| Clé/Stream | Accès | Raison |
|-----------|-------|--------|
| `~relais:messages:incoming` | READ + XACK | Consumer group `commandant_group` |
| `~relais:messages:outgoing:*` | WRITE | Publier les réponses de confirmation |
| `~relais:memory:request` | WRITE | Envoyer `action="clear"` à Souvenir |
| `~relais:state:*` | READ + WRITE | SET/DEL `relais:state:dnd` |
| `~relais:logs` | WRITE | Logs structurés |

### Mise à jour de l'utilisateur `portail`

Remplacer :
```
user portail on >pass_portail ~relais:messages:* ~relais:security ~relais:tasks ~relais:active_sessions:* ~relais:logs +@all
```

Par :
```
user portail on >pass_portail ~relais:messages:* ~relais:security ~relais:tasks ~relais:active_sessions:* ~relais:logs ~relais:state:* +@all
```

**Raison :** Portail doit pouvoir lire `relais:state:dnd` pour le check DND (Phase 3).

---

## 3. Variables d'environnement

Ajouter dans `.env` (ou `.env.example`) :

```bash
REDIS_PASS_COMMANDANT=pass_commandant
```

**Note :** `RedisClient("commandant")` lit automatiquement `REDIS_PASS_COMMANDANT` via la convention de nommage existante dans `common/redis_client.py`.

---

## 4. Vérification post-déploiement

```bash
# Recharger supervisord sans arrêter les autres services
supervisorctl -c supervisord.conf reread
supervisorctl -c supervisord.conf update

# Vérifier que commandant tourne
supervisorctl -c supervisord.conf status commandant

# Vérifier les logs de démarrage
supervisorctl -c supervisord.conf tail commandant

# Test manuel rapide via Discord
# Envoyer "/dnd" → attendre confirmation
# Envoyer un message normal → il doit être ignoré (pas de réponse LLM)
# Envoyer "/brb" → attendre confirmation
# Envoyer un message normal → réponse LLM normale

# Vérifier l'état DND dans Redis
redis-cli -s ./.relais/redis.sock -a pass_commandant GET relais:state:dnd
# → (nil) quand inactif, "1" quand actif
```

---

## 5. Résumé des fichiers créés/modifiés

| Fichier | Action |
|---------|--------|
| `commandant/__init__.py` | Créé (vide) |
| `commandant/command_parser.py` | Créé |
| `commandant/handlers.py` | Créé |
| `commandant/main.py` | Créé |
| `portail/main.py` | Modifié (check DND) |
| `souvenir/main.py` | Modifié (action clear) |
| `souvenir/long_term_store.py` | Modifié (clear_session) |
| `supervisord.conf` | Modifié (ajout commandant) |
| `config/redis.conf` | Modifié (ACL commandant + portail) |
| `tests/test_commandant.py` | Créé |
| `tests/test_portail_dnd.py` | Créé |
| `tests/test_souvenir_clear.py` | Créé |
