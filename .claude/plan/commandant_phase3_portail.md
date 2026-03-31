# Phase 3 — Portail : check DND

**Fichier à modifier:** `portail/main.py`
**Complexité:** LOW

---

## Contexte

`portail/main.py` consomme `relais:messages:incoming` via `portail_group`.
La boucle principale est `_process_stream` (ligne 88).
Actuellement, tous les messages sont forwardés vers `relais:security`.

On doit ajouter : **avant le forward**, vérifier si `relais:state:dnd` est set.
Si oui → ACK le message, log, et `continue` (pas de forward).

---

## Modification précise

### Où insérer le check

Dans `_process_stream`, après la ligne :
```python
envelope = Envelope.from_json(payload)
```
(ligne ~128 dans la version actuelle)

Et **avant** :
```python
# Update session mapping
await self._update_session(...)
```

### Code à insérer

```python
# Check DND mode — drop message if active
dnd_active = await redis_conn.get("relais:state:dnd")
if dnd_active:
    logger.info(
        "DND active — dropping message %s from %s",
        envelope.correlation_id,
        envelope.sender_id,
    )
    continue  # Le finally:xack s'exécute quand même
```

### Résultat après modification (extrait du bloc try interne)

```python
try:
    # Parse Envelope
    payload = data.get("payload", "{}")
    envelope = Envelope.from_json(payload)

    logger.info(
        f"Received message: {envelope.correlation_id} "
        f"from {envelope.channel}"
    )

    # ── CHECK DND ─────────────────────────────────────────────────────────
    dnd_active = await redis_conn.get("relais:state:dnd")
    if dnd_active:
        logger.info(
            "DND active — dropping message %s from %s",
            envelope.correlation_id,
            envelope.sender_id,
        )
        continue  # Le finally:xack exécuté par le bloc englobant
    # ──────────────────────────────────────────────────────────────────────

    # Update session mapping
    await self._update_session(
        redis_conn, envelope.sender_id, envelope.channel
    )
    # ... reste identique
```

---

## Comportement DND

| État `relais:state:dnd` | Action Portail |
|------------------------|----------------|
| Clé absente (None) | Forward normal vers `relais:security` |
| Clé présente (valeur `"1"` ou autre) | Drop silencieux + log INFO + ACK |

**Note:** Le Portail ne vérifie pas la valeur, seulement la présence de la clé.
Toute valeur non-nulle est traitée comme DND actif (cohérent avec Redis `SET` / `DEL`).

---

## Mise à jour ACL Redis

Le user `portail` dans `config/redis.conf` doit avoir accès à `relais:state:*`.

ACL actuelle :
```
user portail on >pass_portail ~relais:messages:* ~relais:security ~relais:tasks ~relais:active_sessions:* ~relais:logs +@all
```

ACL après modification :
```
user portail on >pass_portail ~relais:messages:* ~relais:security ~relais:tasks ~relais:active_sessions:* ~relais:logs ~relais:state:* +@all
```

Cette modification est documentée dans la Phase 5.

---

## Tests de régression à vérifier

Après modification, s'assurer que :
```bash
pytest tests/test_portail.py tests/test_portail_dnd.py -v
```

Les tests existants dans `tests/test_portail.py` ne doivent pas régresser.
Le mock `redis.get` doit retourner `None` dans les fixtures existantes pour simuler DND inactif.
