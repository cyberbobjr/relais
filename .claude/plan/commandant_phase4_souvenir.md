# Phase 4 — Souvenir : action clear

**Fichiers à modifier:**
- `souvenir/long_term_store.py` — ajouter `clear_session(session_id)`
- `souvenir/main.py` — ajouter branche `action == "clear"` dans `_process_request_stream`

**Complexité:** LOW

---

## 1. `souvenir/long_term_store.py` — Nouvelle méthode `clear_session`

### Responsabilité
Supprimer tous les `ArchivedMessage` dont `session_id` correspond.
Les `UserFact` et `Memory` ne sont PAS touchés.

### Position dans le fichier
Ajouter après la méthode `get_recent_messages` (ligne ~318) et avant `query`.

### Spec

```python
async def clear_session(self, session_id: str) -> int:
    """Supprime tous les messages archivés d'une session SQLite.
    
    Seule la table ``archived_messages`` est affectée. Les ``UserFact``
    (table ``user_facts``) et les ``Memory`` (table ``memories``) sont
    conservés.
    
    Args:
        session_id: Identifiant de la session à effacer.
        
    Returns:
        Nombre de lignes supprimées.
    """
    from sqlalchemy import delete as sa_delete
    
    stmt = sa_delete(ArchivedMessage).where(
        ArchivedMessage.session_id == session_id
    )
    async with self._session_factory() as session:
        result = await session.execute(stmt)
        await session.commit()
    
    deleted_count: int = result.rowcount
    logger.info(
        "Cleared %d archived messages for session=%s",
        deleted_count,
        session_id,
    )
    return deleted_count
```

### Import nécessaire
`sa_delete` est importé localement dans la méthode pour éviter le conflit de noms avec `self.delete` (méthode `Memory`). Pas de modification des imports en tête de fichier nécessaire.

---

## 2. `souvenir/main.py` — Branche `action == "clear"`

### Position dans le code
Dans `_process_request_stream`, dans le bloc `if/elif` d'actions (lignes ~206-228).

### Modification

Remplacer :
```python
elif action == "store_memory":
    ...
else:
    logger.warning("Unknown memory action: %s", action)
```

Par :
```python
elif action == "clear":
    await context_store.clear(session_id)
    await self._long_term.clear_session(session_id)
    logger.info(
        "Cleared context for session=%s (Redis + SQLite)",
        session_id,
    )

elif action == "store_memory":
    ...

else:
    logger.warning("Unknown memory action: %s", action)
```

### Payload attendu sur `relais:memory:request`

```json
{
    "action": "clear",
    "session_id": "session_abc",
    "correlation_id": "corr_001"
}
```

Seul `session_id` est utilisé. `correlation_id` est présent pour la traçabilité des logs mais n'est pas exploité par le handler `clear`.

---

## Résultat final après `/clear`

| Store | Avant `/clear` | Après `/clear` |
|-------|---------------|---------------|
| Redis `relais:context:{session_id}` | Messages (liste) | Supprimé (`DEL`) |
| SQLite `archived_messages` WHERE `session_id=?` | Lignes | Supprimées |
| SQLite `user_facts` | Faits | **Conservés** |
| SQLite `memories` (Memory) | Données | **Conservés** |

---

## Tests à vérifier

```bash
pytest tests/test_souvenir_clear.py tests/test_souvenir.py -v
```

S'assurer que les tests existants ne régressent pas (notamment les fixtures qui mock `_long_term` ne doivent pas s'attendre à `clear_session` manquant).
