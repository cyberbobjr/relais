"""Tests for Forgeron auto-creation pipeline.

Covers IntentLabeler, SessionStore, SkillCreator, and Forgeron main.py
orchestration without making real LLM calls.
"""

import json
import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from forgeron.intent_labeler import IntentLabeler
from forgeron.session_store import SessionStore
from forgeron.skill_creator import SkillCreator, SkillCreationResult
from common.envelope_actions import ACTION_MESSAGE_OUTGOING_PENDING
from common.profile_loader import ProfileConfig, ResilienceConfig
from common.streams import STREAM_OUTGOING_PENDING


def _make_profile() -> ProfileConfig:
    return ProfileConfig(
        model="test:test",
        temperature=0,
        max_tokens=100,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        base_url=None,
        api_key_env=None,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_store(tmp_path: Path) -> SessionStore:
    """SessionStore backed by a temporary SQLite file."""
    store = SessionStore(db_path=tmp_path / "test_forgeron.db")
    await store._create_tables()
    yield store
    await store.close()


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.ttl = AsyncMock(return_value=-2)  # no cooldown
    redis.set = AsyncMock()
    redis.xadd = AsyncMock()
    return redis


# ---------------------------------------------------------------------------
# 1. IntentLabeler — extract human messages (no LLM)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_intent_labeler_extract_human_messages() -> None:
    """_extract_user_messages correctly picks up type='human' messages."""
    labeler = IntentLabeler(profile=_make_profile())

    messages = [
        {"type": "human", "content": "Send an email to Alice"},
        {"type": "ai", "content": "Sure, I'll send it."},
        {"type": "human", "content": "Also attach the PDF"},
        {"type": "tool", "content": "tool result"},
    ]
    result = labeler._extract_user_messages(messages)
    assert result == ["Send an email to Alice", "Also attach the PDF"]


@pytest.mark.unit
def test_intent_labeler_extract_langchain_id_format() -> None:
    """_extract_user_messages handles LangChain id=[..., 'HumanMessage'] format."""
    labeler = IntentLabeler(profile=_make_profile())

    messages = [
        {
            "id": ["langchain_core", "messages", "HumanMessage"],
            "content": "Summarize this PDF",
        },
        {
            "id": ["langchain_core", "messages", "AIMessage"],
            "content": "Here is the summary.",
        },
    ]
    result = labeler._extract_user_messages(messages)
    assert result == ["Summarize this PDF"]


@pytest.mark.unit
def test_intent_labeler_extract_empty_returns_empty() -> None:
    """_extract_user_messages returns [] when no human messages exist."""
    labeler = IntentLabeler(profile=_make_profile())

    messages = [
        {"type": "ai", "content": "I can help."},
        {"type": "tool", "content": "result"},
    ]
    result = labeler._extract_user_messages(messages)
    assert result == []


# ---------------------------------------------------------------------------
# 2. SessionStore — record and count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_session_store_record_and_count(session_store: SessionStore, mock_redis: AsyncMock) -> None:
    """Three sessions with the same intent_label → SkillProposal.session_count == 3."""
    for i in range(3):
        await session_store.record_session(
            session_id=f"sess-{i}",
            correlation_id=f"corr-{i}",
            channel="discord",
            sender_id="discord:123",
            intent_label="send_email",
            user_content_preview=f"Send email {i}",
        )

    proposal = await session_store.get_proposal("send_email")
    assert proposal is not None
    assert proposal.session_count == 3
    assert proposal.status == "pending"
    assert proposal.intent_label == "send_email"


# ---------------------------------------------------------------------------
# 3. SessionStore — should_create returns False below threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_session_store_should_create_false_below_threshold(
    session_store: SessionStore, mock_redis: AsyncMock
) -> None:
    """should_create returns False when session_count < min_sessions."""
    await session_store.record_session(
        session_id="sess-1",
        correlation_id="corr-1",
        channel="discord",
        sender_id="discord:1",
        intent_label="search_web",
        user_content_preview="Search for cats",
    )

    result = await session_store.should_create("search_web", min_sessions=3, redis_conn=mock_redis)
    assert result is False


# ---------------------------------------------------------------------------
# 4. SessionStore — should_create returns True at threshold (no cooldown)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_session_store_should_create_true_at_threshold(
    session_store: SessionStore, mock_redis: AsyncMock
) -> None:
    """should_create returns True when threshold is met and no cooldown."""
    mock_redis.ttl = AsyncMock(return_value=-2)  # no cooldown

    for i in range(3):
        await session_store.record_session(
            session_id=f"sess-{i}",
            correlation_id=f"corr-{i}",
            channel="discord",
            sender_id="discord:1",
            intent_label="create_event",
            user_content_preview=f"Create event {i}",
        )

    result = await session_store.should_create("create_event", min_sessions=3, redis_conn=mock_redis)
    assert result is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_session_store_should_create_false_when_cooldown(
    session_store: SessionStore, mock_redis: AsyncMock
) -> None:
    """should_create returns False when Redis cooldown key is active."""
    mock_redis.ttl = AsyncMock(return_value=3600)  # active cooldown

    for i in range(5):
        await session_store.record_session(
            session_id=f"sess-{i}",
            correlation_id=f"corr-{i}",
            channel="discord",
            sender_id="discord:1",
            intent_label="summarize_pdf",
            user_content_preview=f"Summarize {i}",
        )

    result = await session_store.should_create("summarize_pdf", min_sessions=3, redis_conn=mock_redis)
    assert result is False


# ---------------------------------------------------------------------------
# 5. SkillCreator — extract description
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 6. SkillCreator — skips existing skill (idempotence)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_skill_creator_skips_existing_skill(tmp_path: Path) -> None:
    """create() returns None without writing when SKILL.md already exists."""
    creator = SkillCreator(profile=_make_profile(), skills_dir=tmp_path)

    # Pre-create the skill
    skill_dir = tmp_path / "send-email"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("existing content", encoding="utf-8")

    result = await creator.create("send_email", [{"user_content_preview": "Send email"}])
    assert result is None
    # Content unchanged
    assert (skill_dir / "SKILL.md").read_text() == "existing content"


# ---------------------------------------------------------------------------
# 7. Forgeron._process_archive — skip when envelope_json is absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_forgeron_handle_archive_no_envelope_json(mock_redis: AsyncMock) -> None:
    """_process_archive skips silently when envelope_json is missing."""
    from forgeron.main import Forgeron
    from common.envelope import Envelope
    from common.contexts import CTX_SOUVENIR_REQUEST

    forgeron = Forgeron()

    env = Envelope(
        content="",
        sender_id="atelier:discord:123",
        channel="internal",
        session_id="sess-x",
        correlation_id="corr-x",
    )
    # No CTX_SOUVENIR_REQUEST at all
    await forgeron._process_archive(env, mock_redis)
    # Should not raise and should not call xadd
    mock_redis.xadd.assert_not_called()


# ---------------------------------------------------------------------------
# 8. Forgeron._notify_user — publishes to STREAM_OUTGOING_PENDING
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_forgeron_notify_user_publishes_to_outgoing_pending(mock_redis: AsyncMock) -> None:
    """_notify_user calls xadd(STREAM_OUTGOING_PENDING) with correct action."""
    import json as _json
    from forgeron.main import Forgeron

    forgeron = Forgeron()
    await forgeron._notify_user(
        channel="discord",
        sender_id="discord:999",
        session_id="sess-1",
        correlation_id="corr-1",
        message="[Forgeron] Test notification",
        redis_conn=mock_redis,
    )

    # xadd on STREAM_OUTGOING_PENDING (STREAM_LOGS now via BrickLogger)
    outgoing_calls = [c for c in mock_redis.xadd.call_args_list if c[0][0] == STREAM_OUTGOING_PENDING]
    assert len(outgoing_calls) == 1
    call_args = outgoing_calls[0]
    payload_str = call_args[0][1]["payload"]

    from common.envelope import Envelope
    notif_env = Envelope.from_json(payload_str)
    assert notif_env.action == ACTION_MESSAGE_OUTGOING_PENDING
    assert notif_env.content == "[Forgeron] Test notification"
    assert notif_env.channel == "discord"
    assert notif_env.sender_id == "discord:999"


# ---------------------------------------------------------------------------
# 9. Forgeron.stream_specs — has exactly two consumer specs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_forgeron_stream_specs_has_two_consumers() -> None:
    """stream_specs() returns 2 StreamSpecs, including forgeron_archive_group."""
    from forgeron.main import Forgeron
    from common.streams import STREAM_MEMORY_REQUEST

    forgeron = Forgeron()
    specs = forgeron.stream_specs()

    assert len(specs) == 2
    streams = {s.stream for s in specs}
    assert STREAM_MEMORY_REQUEST in streams

    archive_spec = next(s for s in specs if s.stream == STREAM_MEMORY_REQUEST)
    assert archive_spec.group == "forgeron_archive_group"
