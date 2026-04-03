"""Unit tests for the Souvenir refactoring — TDD RED first.

Tests the new schema and behavior:
- ArchivedMessage: new columns (user_content, assistant_content, messages_raw),
  removed columns (role, content).
- LongTermStore.archive(envelope, messages_raw) — new signature.
- LongTermStore.get_recent_messages(user_id, limit) — returns flat list[dict]
  from deserialized messages_raw blobs.
- ContextStore.append_turn(session_id, messages_raw) — new signature, stores
  a single JSON blob per turn.
- ContextStore.get_recent(session_id, limit) — deserializes each blob and
  flattens.
- Souvenir._handle_outgoing() — reads messages_raw from envelope.metadata.
- AgentResult dataclass — reply_text + messages_raw.
- AgentExecutor.execute() returns AgentResult.
- Atelier._handle_message() attaches messages_raw to response_env.metadata.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from common.envelope import Envelope
from souvenir.context_store import ContextStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(
    content: str = "assistant reply",
    sender_id: str = "user_x",
    session_id: str = "sess_x",
    messages_raw: list[dict] | None = None,
    user_message: str = "user question",
) -> Envelope:
    """Create a test envelope with optional messages_raw in metadata."""
    metadata: dict = {"user_message": user_message}
    if messages_raw is not None:
        metadata["messages_raw"] = messages_raw
    return Envelope(
        content=content,
        sender_id=sender_id,
        channel="discord",
        session_id=session_id,
        metadata=metadata,
    )


_SAMPLE_MESSAGES_RAW: list[dict] = [
    {"role": "human", "content": "user question"},
    {"role": "ai", "content": "assistant reply"},
]


# ---------------------------------------------------------------------------
# Phase 1: UserFact model removed from models.py
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_user_fact_model_removed_from_models() -> None:
    """UserFact must no longer exist in souvenir.models after refactoring."""
    import souvenir.models as m

    assert not hasattr(m, "UserFact"), (
        "UserFact model must be removed from souvenir.models"
    )


@pytest.mark.unit
def test_upsert_facts_removed_from_long_term_store() -> None:
    """LongTermStore must not expose upsert_facts() after refactoring."""
    from souvenir.long_term_store import LongTermStore

    assert not hasattr(LongTermStore, "upsert_facts"), (
        "upsert_facts() must be removed from LongTermStore"
    )


@pytest.mark.unit
def test_get_user_facts_removed_from_long_term_store() -> None:
    """LongTermStore must not expose get_user_facts() after refactoring."""
    from souvenir.long_term_store import LongTermStore

    assert not hasattr(LongTermStore, "get_user_facts"), (
        "get_user_facts() must be removed from LongTermStore"
    )


# ---------------------------------------------------------------------------
# Phase 2: ArchivedMessage — new schema
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_archived_message_has_messages_raw_column() -> None:
    """ArchivedMessage must have a 'messages_raw' column (JSON blob)."""
    from souvenir.models import ArchivedMessage

    # Check the SQLModel field exists
    fields = ArchivedMessage.model_fields
    assert "messages_raw" in fields, (
        "ArchivedMessage must have a 'messages_raw' field"
    )


@pytest.mark.unit
def test_archived_message_has_user_content_column() -> None:
    """ArchivedMessage must have a 'user_content' column."""
    from souvenir.models import ArchivedMessage

    fields = ArchivedMessage.model_fields
    assert "user_content" in fields, (
        "ArchivedMessage must have a 'user_content' field"
    )


@pytest.mark.unit
def test_archived_message_has_assistant_content_column() -> None:
    """ArchivedMessage must have an 'assistant_content' column."""
    from souvenir.models import ArchivedMessage

    fields = ArchivedMessage.model_fields
    assert "assistant_content" in fields, (
        "ArchivedMessage must have an 'assistant_content' field"
    )


@pytest.mark.unit
def test_archived_message_has_no_role_column() -> None:
    """ArchivedMessage must NOT have a 'role' column after refactoring."""
    from souvenir.models import ArchivedMessage

    fields = ArchivedMessage.model_fields
    assert "role" not in fields, (
        "ArchivedMessage must not have a 'role' field (clean break)"
    )


@pytest.mark.unit
def test_archived_message_has_no_content_column() -> None:
    """ArchivedMessage must NOT have a 'content' column after refactoring."""
    from souvenir.models import ArchivedMessage

    fields = ArchivedMessage.model_fields
    assert "content" not in fields, (
        "ArchivedMessage must not have a 'content' field (clean break)"
    )


@pytest.mark.unit
def test_archived_message_correlation_id_is_unique() -> None:
    """ArchivedMessage.correlation_id must be declared unique (one row per turn)."""
    from souvenir.models import ArchivedMessage
    from sqlalchemy import inspect as sa_inspect

    # Check SQLAlchemy UniqueConstraint or unique=True on the column
    mapper = ArchivedMessage.__mapper__
    columns = {c.name: c for c in mapper.columns}
    corr_col = columns.get("correlation_id")
    assert corr_col is not None
    # Either unique=True on the column or a UniqueConstraint in __table_args__
    table = ArchivedMessage.__table__
    is_unique_col = corr_col.unique
    is_unique_constraint = any(
        "correlation_id" in [c.name for c in uc.columns]
        for uc in table.constraints
        if hasattr(uc, "columns")
    )
    assert is_unique_col or is_unique_constraint, (
        "correlation_id must be unique in ArchivedMessage"
    )


# ---------------------------------------------------------------------------
# Phase 3: LongTermStore.archive() — new signature
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def lts(tmp_path: Path):
    """LongTermStore with fresh schema in a temp file."""
    from souvenir.long_term_store import LongTermStore

    store = LongTermStore(db_path=tmp_path / "test.db")
    await store._create_tables()
    yield store
    await store.close()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_archive_stores_messages_raw_blob(lts) -> None:
    """archive(envelope, messages_raw) must persist the messages_raw JSON blob."""
    from souvenir.long_term_store import LongTermStore
    from souvenir.models import ArchivedMessage
    from sqlmodel import select

    env = _make_envelope(messages_raw=_SAMPLE_MESSAGES_RAW)
    await lts.archive(env, _SAMPLE_MESSAGES_RAW)

    async with lts._session_factory() as session:
        result = await session.exec(select(ArchivedMessage))
        rows = result.all()

    assert len(rows) == 1
    stored = json.loads(rows[0].messages_raw)
    assert stored == _SAMPLE_MESSAGES_RAW


@pytest.mark.asyncio
@pytest.mark.unit
async def test_archive_stores_user_content_and_assistant_content(lts) -> None:
    """archive() must store user_content and assistant_content for searchability."""
    from souvenir.models import ArchivedMessage
    from sqlmodel import select

    env = _make_envelope(
        content="voici ma réponse",
        user_message="quelle est la réponse?",
        messages_raw=_SAMPLE_MESSAGES_RAW,
    )
    await lts.archive(env, _SAMPLE_MESSAGES_RAW)

    async with lts._session_factory() as session:
        result = await session.exec(select(ArchivedMessage))
        row = result.first()

    assert row.user_content == "quelle est la réponse?"
    assert row.assistant_content == "voici ma réponse"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_archive_upsert_on_correlation_id(lts) -> None:
    """archive() with the same correlation_id must UPDATE, not INSERT a duplicate."""
    from souvenir.models import ArchivedMessage
    from sqlmodel import select

    env = _make_envelope()
    env2 = _make_envelope(content="updated reply")
    # Force same correlation_id
    import dataclasses
    env2 = dataclasses.replace(env2, correlation_id=env.correlation_id)

    msgs2 = [{"role": "human", "content": "q"}, {"role": "ai", "content": "updated reply"}]

    await lts.archive(env, _SAMPLE_MESSAGES_RAW)
    await lts.archive(env2, msgs2)

    async with lts._session_factory() as session:
        result = await session.exec(select(ArchivedMessage))
        rows = result.all()

    assert len(rows) == 1
    stored = json.loads(rows[0].messages_raw)
    assert stored == msgs2


# ---------------------------------------------------------------------------
# Phase 4: LongTermStore.get_recent_messages() — returns flat deserialized list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_recent_messages_returns_flat_message_list(lts) -> None:
    """get_recent_messages() must return the flattened list of message dicts."""
    msgs = [
        {"role": "human", "content": "q1"},
        {"role": "ai", "content": "a1"},
        {"role": "human", "content": "q2"},
        {"role": "ai", "content": "a2", "tool_calls": []},
    ]
    env = _make_envelope(messages_raw=msgs, session_id="sess_recent")
    await lts.archive(env, msgs)

    result = await lts.get_recent_messages("sess_recent", limit=20)

    # Should return the flat message list from the blob
    assert isinstance(result, list)
    assert len(result) >= 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_recent_messages_returns_empty_for_unknown_session(lts) -> None:
    """get_recent_messages() returns [] for a session with no archived messages."""
    result = await lts.get_recent_messages("no_such_session", limit=20)

    assert result == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_recent_messages_respects_limit(lts) -> None:
    """get_recent_messages() should respect the limit parameter."""
    # Archive multiple turns
    import dataclasses
    import uuid

    base_env = _make_envelope(session_id="sess_limit")
    for i in range(5):
        msgs = [
            {"role": "human", "content": f"q{i}"},
            {"role": "ai", "content": f"a{i}"},
        ]
        env_i = dataclasses.replace(
            base_env,
            content=f"a{i}",
            correlation_id=str(uuid.uuid4()),
            metadata={"user_message": f"q{i}", "messages_raw": msgs},
        )
        await lts.archive(env_i, msgs)

    result = await lts.get_recent_messages("sess_limit", limit=3)

    # limit=3 means at most 3 turns (or 6 messages, but we expect no more than limit)
    assert len(result) <= 6  # 3 turns * 2 messages each


# ---------------------------------------------------------------------------
# Phase 5: ContextStore.append_turn() — new signature (messages_raw blob)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Return an AsyncMock simulating a redis.asyncio.Redis client."""
    redis = AsyncMock()
    redis.rpush = AsyncMock()
    redis.ltrim = AsyncMock()
    redis.expire = AsyncMock()
    redis.lrange = AsyncMock(return_value=[])
    redis.delete = AsyncMock()
    return redis


@pytest.fixture
def context_store(mock_redis: AsyncMock) -> ContextStore:
    """Return a ContextStore wired to the mock Redis."""
    return ContextStore(redis=mock_redis, max_messages=20, ttl_seconds=86400)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_append_turn_with_messages_raw_stores_single_blob(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """append_turn(session_id, messages_raw) must store a single JSON blob per turn."""
    await context_store.append_turn(
        session_id="sess-blob",
        messages_raw=_SAMPLE_MESSAGES_RAW,
    )

    mock_redis.rpush.assert_awaited_once()
    call_args = mock_redis.rpush.call_args[0]
    key = call_args[0]
    blob = call_args[1]

    assert key == "relais:context:sess-blob"
    # Only ONE item pushed (single blob, not two separate items)
    assert len(call_args) == 2  # key + 1 blob
    # Blob must be valid JSON that deserializes to the messages_raw
    parsed = json.loads(blob)
    assert parsed == _SAMPLE_MESSAGES_RAW


@pytest.mark.asyncio
@pytest.mark.unit
async def test_append_turn_with_messages_raw_calls_ltrim_and_expire(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """append_turn() must LTRIM and EXPIRE after pushing the blob."""
    await context_store.append_turn(
        session_id="sess-trim",
        messages_raw=_SAMPLE_MESSAGES_RAW,
    )

    mock_redis.ltrim.assert_awaited_once_with("relais:context:sess-trim", -20, -1)
    mock_redis.expire.assert_awaited_once_with("relais:context:sess-trim", 86400)


# ---------------------------------------------------------------------------
# Phase 6: ContextStore.get_recent() — deserializes blobs and flattens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_recent_deserializes_blobs_and_flattens(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """get_recent() must deserialize each blob and return a flattened list of message dicts."""
    # Two turns stored as blobs
    turn1 = [{"role": "human", "content": "q1"}, {"role": "ai", "content": "a1"}]
    turn2 = [{"role": "human", "content": "q2"}, {"role": "ai", "content": "a2"}]
    mock_redis.lrange.return_value = [
        json.dumps(turn1).encode(),
        json.dumps(turn2).encode(),
    ]

    result = await context_store.get_recent("sess-flat", limit=20)

    assert len(result) == 4
    assert result[0] == {"role": "human", "content": "q1"}
    assert result[1] == {"role": "ai", "content": "a1"}
    assert result[2] == {"role": "human", "content": "q2"}
    assert result[3] == {"role": "ai", "content": "a2"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_recent_returns_empty_when_no_blobs(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """get_recent() returns [] when the Redis list is empty."""
    mock_redis.lrange.return_value = []

    result = await context_store.get_recent("sess-empty", limit=20)

    assert result == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_recent_skips_malformed_blobs(
    context_store: ContextStore, mock_redis: AsyncMock
) -> None:
    """get_recent() silently skips blobs that are not valid JSON."""
    good_blob = json.dumps([{"role": "human", "content": "hello"}]).encode()
    bad_blob = b"not-valid-json{{{"

    mock_redis.lrange.return_value = [good_blob, bad_blob]

    result = await context_store.get_recent("sess-corrupted", limit=20)

    # Only messages from the good blob
    assert len(result) == 1
    assert result[0]["content"] == "hello"


# ---------------------------------------------------------------------------
# Phase 7: Souvenir._handle_outgoing() — reads messages_raw from metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_outgoing_uses_messages_raw_from_metadata() -> None:
    """_handle_outgoing() must read messages_raw from envelope.metadata and pass it
    to context_store.append_turn() and long_term_store.archive()."""
    from souvenir.main import Souvenir

    env = _make_envelope(messages_raw=_SAMPLE_MESSAGES_RAW)

    souvenir = Souvenir.__new__(Souvenir)
    context_store = AsyncMock(spec=ContextStore)
    context_store.append_turn = AsyncMock()
    long_term_store = AsyncMock()
    long_term_store.archive = AsyncMock()

    await souvenir._handle_outgoing(
        envelope=env,
        context_store=context_store,
        long_term_store=long_term_store,
    )

    # context_store.append_turn must be called with messages_raw
    context_store.append_turn.assert_awaited_once()
    call_kwargs = context_store.append_turn.call_args
    assert call_kwargs is not None

    # long_term_store.archive must be called with envelope + messages_raw
    long_term_store.archive.assert_awaited_once()
    archive_args = long_term_store.archive.call_args[0]
    assert archive_args[0] is env
    assert archive_args[1] == _SAMPLE_MESSAGES_RAW


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_outgoing_uses_empty_list_when_no_messages_raw() -> None:
    """_handle_outgoing() must use [] when messages_raw is absent from metadata."""
    from souvenir.main import Souvenir

    # Envelope without messages_raw in metadata
    env = Envelope(
        content="reply",
        sender_id="user_y",
        channel="discord",
        session_id="sess_y",
        metadata={"user_message": "question"},
    )

    souvenir = Souvenir.__new__(Souvenir)
    context_store = AsyncMock(spec=ContextStore)
    context_store.append_turn = AsyncMock()
    long_term_store = AsyncMock()
    long_term_store.archive = AsyncMock()

    await souvenir._handle_outgoing(
        envelope=env,
        context_store=context_store,
        long_term_store=long_term_store,
    )

    # Must not raise — archive called with empty list
    long_term_store.archive.assert_awaited_once()
    archive_args = long_term_store.archive.call_args[0]
    assert archive_args[1] == []


# ---------------------------------------------------------------------------
# Phase 8: AgentResult dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_agent_result_dataclass_exists() -> None:
    """AgentResult must be importable from atelier.agent_executor."""
    from atelier.agent_executor import AgentResult

    assert AgentResult is not None


@pytest.mark.unit
def test_agent_result_has_reply_text_and_messages_raw() -> None:
    """AgentResult must have reply_text (str) and messages_raw (list[dict]) fields."""
    from atelier.agent_executor import AgentResult

    result = AgentResult(
        reply_text="Hello!",
        messages_raw=[{"role": "human", "content": "Hi"}],
    )

    assert result.reply_text == "Hello!"
    assert result.messages_raw == [{"role": "human", "content": "Hi"}]


@pytest.mark.unit
def test_agent_result_is_frozen_dataclass() -> None:
    """AgentResult must be a frozen dataclass (immutable)."""
    import dataclasses
    from atelier.agent_executor import AgentResult

    result = AgentResult(reply_text="text", messages_raw=[])

    assert dataclasses.is_dataclass(result)
    with pytest.raises((AttributeError, TypeError)):
        result.reply_text = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Phase 9: AgentExecutor.execute() — returns AgentResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_agent_executor_execute_returns_agent_result() -> None:
    """AgentExecutor.execute() must return an AgentResult, not a plain str."""
    from atelier.agent_executor import AgentExecutor, AgentResult
    from atelier.profile_loader import ProfileConfig, ResilienceConfig

    profile = ProfileConfig(
        model="anthropic:test-model",
        temperature=0.5,
        max_tokens=512,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        base_url=None,
        api_key_env=None,
    )

    env = Envelope(
        content="hello",
        sender_id="user_z",
        channel="discord",
        session_id="sess_z",
        metadata={},
    )

    # Mock create_deep_agent and agent.astream
    mock_agent = MagicMock()
    mock_state = {"messages": [
        MagicMock(type="human", content="hello"),
        MagicMock(type="ai", content="world"),
    ]}
    mock_agent.aget_state = AsyncMock(return_value=mock_state)

    async def fake_astream(*args, **kwargs):
        # Yield a single AIMessage chunk
        chunk = {
            "type": "messages",
            "ns": [],
            "data": (
                MagicMock(
                    type="ai",
                    content="world",
                    tool_call_chunks=None,
                ),
                {},
            ),
        }
        yield chunk

    mock_agent.astream = fake_astream

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent), \
         patch("atelier.agent_executor.CompositeBackend"), \
         patch("atelier.agent_executor.LocalShellBackend"):
        executor = AgentExecutor(profile=profile, soul_prompt="test", tools=[])
        result = await executor.execute(envelope=env, context=[])

    assert isinstance(result, AgentResult), (
        f"execute() must return AgentResult, got {type(result)}"
    )
    assert isinstance(result.reply_text, str)
    assert isinstance(result.messages_raw, list)


# ---------------------------------------------------------------------------
# Phase 10: Atelier._handle_message() — attaches messages_raw to response_env
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_message_attaches_messages_raw_to_response_envelope() -> None:
    """Atelier._handle_message() must attach result.messages_raw to
    response_env.metadata['messages_raw'] before publishing to outgoing_pending."""
    from atelier.main import Atelier
    from atelier.agent_executor import AgentResult

    env = Envelope(
        content="test question",
        sender_id="discord:u1",
        channel="discord",
        session_id="sess_atelier",
        correlation_id="corr_atelier",
        metadata={"user_record": {}, "user_id": "usr_1"},
    )

    expected_messages_raw = [
        {"role": "human", "content": "test question"},
        {"role": "ai", "content": "test answer"},
    ]
    mock_result = AgentResult(
        reply_text="test answer",
        messages_raw=expected_messages_raw,
    )

    atelier = Atelier.__new__(Atelier)
    atelier._profiles = {}
    atelier._mcp_servers_default = {}
    atelier._progress_config = MagicMock()
    atelier._streaming_capable_channels = frozenset()
    atelier._skills_base_dir = Path("/tmp/skills")

    published_payloads: list[dict] = []

    async def mock_xadd(stream: str, data: dict) -> bytes:
        published_payloads.append({"stream": stream, "data": data})
        return b"1234567890-0"

    redis_conn = AsyncMock()
    redis_conn.xadd = mock_xadd

    with patch("atelier.main.load_profiles", return_value={"default": MagicMock(model="test", temperature=0.5, max_tokens=512)}), \
         patch("atelier.main.resolve_profile", return_value=MagicMock(model="test")), \
         patch.object(atelier, "_fetch_context", return_value=[]), \
         patch("atelier.main.assemble_system_prompt", return_value="soul"), \
         patch("atelier.main.load_for_sdk", return_value={}), \
         patch("atelier.main.McpSessionManager"), \
         patch("atelier.main.AgentExecutor") as MockExecutor, \
         patch("atelier.main.StreamPublisher") as MockPublisher, \
         patch("atelier.main.SouvenirBackend"), \
         patch("atelier.main.ToolPolicy") as MockToolPolicy:

        mock_executor_instance = AsyncMock()
        mock_executor_instance.execute = AsyncMock(return_value=mock_result)
        MockExecutor.return_value = mock_executor_instance

        mock_publisher_instance = AsyncMock()
        mock_publisher_instance.push_chunk = AsyncMock()
        mock_publisher_instance.push_progress = AsyncMock()
        MockPublisher.return_value = mock_publisher_instance

        mock_tool_policy = MagicMock()
        mock_tool_policy.resolve_skills.return_value = []
        mock_tool_policy.parse_mcp_patterns.return_value = []
        mock_tool_policy.filter_mcp_tools.return_value = []
        MockToolPolicy.return_value = mock_tool_policy

        # Mock McpSessionManager as context manager
        mock_session_mgr = MagicMock()
        mock_session_mgr.start_all = AsyncMock()
        from atelier.main import McpSessionManager
        with patch("atelier.main.McpSessionManager", return_value=mock_session_mgr):
            result = await atelier._handle_message(
                redis_conn=redis_conn,
                message_id="msg-1",
                payload=env.to_json(),
            )

    assert result is True

    # Find the outgoing_pending publish
    outgoing_publishes = [
        p for p in published_payloads
        if p["stream"] == "relais:messages:outgoing_pending"
    ]
    assert len(outgoing_publishes) == 1, "Must publish exactly once to outgoing_pending"

    response_payload = json.loads(outgoing_publishes[0]["data"]["payload"])
    assert "messages_raw" in response_payload.get("metadata", {}), (
        "response_env.metadata must contain 'messages_raw'"
    )
    assert response_payload["metadata"]["messages_raw"] == expected_messages_raw


# ---------------------------------------------------------------------------
# Phase 11: profiles.yaml.default — memory_extractor removed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_profiles_yaml_default_has_no_memory_extractor() -> None:
    """profiles.yaml.default must NOT contain a 'memory_extractor' profile after cleanup."""
    from atelier.profile_loader import load_profiles

    _DEFAULT_PROFILES_PATH = (
        Path(__file__).parent.parent / "config" / "atelier" / "profiles.yaml.default"
    )
    profiles = load_profiles(config_path=_DEFAULT_PROFILES_PATH)

    assert "memory_extractor" not in profiles, (
        "memory_extractor profile must be removed from profiles.yaml.default"
    )
