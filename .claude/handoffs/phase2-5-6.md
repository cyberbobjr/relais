# Handoff — Phase 2.5 & 2.6

Date: 2026-03-27

## Fichiers créés

### souvenir/context_store.py
Redis List, clé `relais:context:{session_id}`, fenêtre glissante RPUSH+LTRIM(max=20), TTL 24 h.
Méthodes : `append`, `get`, `clear`, `get_session_ids`.

### souvenir/long_term_store.py
SQLite via `aiosqlite` (stdlib `sqlite3` + `aiosqlite`), chemin par défaut `~/.relais/memory.db`.
Schéma auto-créé à la première utilisation (`_ensure_schema`).
Méthodes : `store` (upsert), `retrieve`, `delete`, `search` (LIKE).

### archiviste/cleanup_retention.py
`RetentionConfig` dataclass (jsonl_days=90, sqlite_days=365, audit_days=None).
`CleanupManager` : `cleanup_jsonl`, `get_stats`, `run_daily`.

## pyproject.toml — dépendances ajoutées
- httpx >= 0.27
- aiosqlite >= 0.20
- pyyaml >= 6.0
- python-dotenv >= 1.0
- pydantic >= 2.9

sqlmodel et alembic non ajoutés (phase ultérieure).

## Contraintes respectées
- sqlite3 stdlib + aiosqlite uniquement (pas sqlmodel/alembic)
- Type hints complets sur toutes les signatures
- Fichiers < 200 lignes
- Docstrings Google Style

## Prochaines étapes suggérées
- Intégrer `ContextStore` dans `souvenir/main.py` en remplacement du code RPUSH inline
- Tester `LongTermStore` avec pytest + `tmp_path` fixture
- Brancher `CleanupManager.run_daily()` dans `veilleur/` (Phase 4.2) via APScheduler
- La contrainte `UNIQUE(user_id, key)` est présente dans le schéma — l'upsert `ON CONFLICT` est fonctionnel
