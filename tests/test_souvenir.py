"""Unit tests for souvenir module: ContextStore and LongTermStore."""

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# ContextStore tests
# ---------------------------------------------------------------------------

from souvenir.context_store import ContextStore


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Return an AsyncMock simulating a redis.asyncio.Redis client."""
    redis = AsyncMock()
    redis.rpush = AsyncMock()
    redis.ltrim = AsyncMock()
    redis.expire = AsyncMock()
    redis.lrange = AsyncMock(return_value=[])
    redis.delete = AsyncMock()
    redis.scan = AsyncMock(return_value=(0, []))
    return redis


@pytest.fixture
def context_store(mock_redis: AsyncMock) -> ContextStore:
    """Return a ContextStore wired to the mock Redis."""
    return ContextStore(redis=mock_redis, max_messages=20, ttl_seconds=86400)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_append_calls_rpush_ltrim_expire(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """append() doit appeler RPUSH, LTRIM et EXPIRE avec les bons arguments."""
    session_id = "sess-001"
    expected_key = f"relais:context:{session_id}"
    expected_entry = json.dumps({"role": "user", "content": "Bonjour"})

    await context_store.append(session_id, "user", "Bonjour")

    mock_redis.rpush.assert_awaited_once_with(expected_key, expected_entry)
    mock_redis.ltrim.assert_awaited_once_with(expected_key, -20, -1)
    mock_redis.expire.assert_awaited_once_with(expected_key, 86400)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_returns_empty_list_for_unknown_session(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """get() doit retourner une liste vide si la session est inconnue."""
    mock_redis.lrange.return_value = []

    result = await context_store.get("session-unknown")

    assert result == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_returns_formatted_messages(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """get() doit désérialiser les entrées JSON en dicts {role, content}."""
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    mock_redis.lrange.return_value = [json.dumps(m).encode() for m in messages]

    result = await context_store.get("sess-abc")

    assert result == messages


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_skips_malformed_json_entries(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """get() doit ignorer silencieusement les entrées JSON corrompues."""
    good = json.dumps({"role": "user", "content": "ok"}).encode()
    bad = b"not-valid-json{{{}"
    mock_redis.lrange.return_value = [good, bad]

    result = await context_store.get("sess-corrupt")

    assert len(result) == 1
    assert result[0]["content"] == "ok"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_clear_calls_delete(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """clear() doit appeler DELETE sur la clé de la session."""
    session_id = "sess-del"
    expected_key = f"relais:context:{session_id}"

    await context_store.clear(session_id)

    mock_redis.delete.assert_awaited_once_with(expected_key)


@pytest.mark.unit
def test_redis_key_pattern(context_store: ContextStore) -> None:
    """La clé Redis doit suivre le pattern relais:context:{session_id}."""
    assert context_store._key("abc123") == "relais:context:abc123"
    assert context_store._key("user-42") == "relais:context:user-42"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_session_ids_returns_empty_when_no_sessions(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """get_session_ids() doit retourner [] si aucune clé n'existe."""
    mock_redis.scan.return_value = (0, [])

    result = await context_store.get_session_ids()

    assert result == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_session_ids_strips_prefix(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """get_session_ids() doit retourner les session_id sans le préfixe."""
    mock_redis.scan.return_value = (
        0,
        [b"relais:context:sess-1", b"relais:context:sess-2"],
    )

    result = await context_store.get_session_ids()

    assert set(result) == {"sess-1", "sess-2"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_session_ids_paginates_via_cursor(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """get_session_ids() doit itérer jusqu'à ce que le curseur soit 0."""
    mock_redis.scan.side_effect = [
        (42, [b"relais:context:sess-a"]),
        (0, [b"relais:context:sess-b"]),
    ]

    result = await context_store.get_session_ids()

    assert set(result) == {"sess-a", "sess-b"}
    assert mock_redis.scan.await_count == 2


# ---------------------------------------------------------------------------
# LongTermStore tests
# ---------------------------------------------------------------------------

from souvenir.long_term_store import LongTermStore


@pytest_asyncio.fixture
async def long_term_store(tmp_path: Path) -> AsyncGenerator[LongTermStore, None]:
    """Return a LongTermStore backed by a temporary SQLite database.

    Calls ``_create_tables()`` to initialise the schema without Alembic
    (test-only pattern). Disposes the async engine on teardown to avoid
    aiosqlite thread leaks.
    """
    store = LongTermStore(db_path=tmp_path / "memory.db")
    await store._create_tables()
    yield store
    await store.close()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_store_inserts_new_memory(long_term_store: LongTermStore) -> None:
    """store() doit insérer un nouveau souvenir."""
    await long_term_store.store("user1", "prénom", "Alice")

    results = await long_term_store.retrieve("user1")
    assert len(results) == 1
    assert results[0]["key"] == "prénom"
    assert results[0]["value"] == "Alice"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_store_upserts_existing_key(long_term_store: LongTermStore) -> None:
    """store() doit mettre à jour la valeur si la clé existe déjà (upsert)."""
    await long_term_store.store("user1", "ville", "Paris")
    await long_term_store.store("user1", "ville", "Lyon")

    results = await long_term_store.retrieve("user1", key="ville")
    assert len(results) == 1
    assert results[0]["value"] == "Lyon"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_retrieve_returns_all_memories_for_user(
    long_term_store: LongTermStore,
) -> None:
    """retrieve() sans filtre doit retourner tous les souvenirs de l'utilisateur."""
    await long_term_store.store("user2", "prénom", "Bob")
    await long_term_store.store("user2", "ville", "Marseille")

    results = await long_term_store.retrieve("user2")
    keys = {r["key"] for r in results}
    assert keys == {"prénom", "ville"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_retrieve_filters_by_key(long_term_store: LongTermStore) -> None:
    """retrieve() avec key doit filtrer sur cette clé exacte."""
    await long_term_store.store("user3", "prénom", "Carol")
    await long_term_store.store("user3", "âge", "30")

    results = await long_term_store.retrieve("user3", key="prénom")
    assert len(results) == 1
    assert results[0]["key"] == "prénom"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_delete_removes_memory(long_term_store: LongTermStore) -> None:
    """delete() doit supprimer le souvenir correspondant à (user_id, key)."""
    await long_term_store.store("user4", "couleur", "bleu")
    await long_term_store.delete("user4", "couleur")

    results = await long_term_store.retrieve("user4", key="couleur")
    assert results == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_search_returns_matching_memories(
    long_term_store: LongTermStore,
) -> None:
    """search() doit retourner les souvenirs dont la valeur contient la query."""
    await long_term_store.store("user5", "note1", "J'aime le café")
    await long_term_store.store("user5", "note2", "J'aime le thé")
    await long_term_store.store("user5", "note3", "Je déteste la pluie")

    results = await long_term_store.search("user5", "aime")
    values = {r["value"] for r in results}
    assert values == {"J'aime le café", "J'aime le thé"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_search_returns_empty_when_no_match(
    long_term_store: LongTermStore,
) -> None:
    """search() doit retourner une liste vide si rien ne correspond."""
    await long_term_store.store("user6", "info", "quelque chose")

    results = await long_term_store.search("user6", "introuvable")
    assert results == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_store_source_variants(long_term_store: LongTermStore) -> None:
    """store() doit persister la valeur du champ source correctement."""
    await long_term_store.store("user7", "k1", "v1", source="llm")
    await long_term_store.store("user7", "k2", "v2", source="auto")
    await long_term_store.store("user7", "k3", "v3")  # default = "manual"

    results = await long_term_store.retrieve("user7")
    sources = {r["key"]: r["source"] for r in results}
    assert sources == {"k1": "llm", "k2": "auto", "k3": "manual"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_create_tables_is_idempotent(tmp_path: Path) -> None:
    """_create_tables() peut être appelé plusieurs fois sans erreur."""
    store = LongTermStore(db_path=tmp_path / "idem.db")
    try:
        await store._create_tables()
        await store._create_tables()  # second call must not raise

        await store.store("u", "k", "v")
        results = await store.retrieve("u")
        assert len(results) == 1
    finally:
        await store.close()


@pytest.mark.unit
def test_long_term_store_default_path_respects_relais_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LongTermStore() sans argument doit utiliser RELAIS_HOME/storage/memory.db."""
    custom_home = tmp_path / "custom_relais"
    monkeypatch.setenv("RELAIS_HOME", str(custom_home))

    # resolve_storage_dir() is called lazily inside __init__, so monkeypatching
    # the env var is sufficient — no module reload required.
    store = LongTermStore()
    assert store._db_path == custom_home / "storage" / "memory.db"
