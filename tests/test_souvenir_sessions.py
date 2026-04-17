"""TDD tests for SessionsHandler and ResumeHandler.

Tests are written FIRST (RED phase) — handlers do not exist yet.
All external dependencies (Redis, SQLite) are mocked.
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_OUTGOING


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orig_envelope(channel: str = "test_channel") -> Envelope:
    """Build a minimal Envelope to embed as envelope_json in ctx.req."""
    env = Envelope(
        content="/sessions",
        sender_id="usr_admin",
        channel=channel,
        session_id="sess_123",
        correlation_id="corr_456",
        action="cmd.sessions",
    )
    return env


def _make_ctx(
    *,
    req: dict,
    list_sessions_return: list | None = None,
    get_session_history_return: list | None = None,
) -> MagicMock:
    """Build a HandlerContext-like MagicMock with controlled store responses."""
    ctx = MagicMock()
    ctx.redis_conn = AsyncMock()
    ctx.redis_conn.xadd = AsyncMock()

    ctx.long_term_store = AsyncMock()
    ctx.long_term_store.list_sessions = AsyncMock(
        return_value=list_sessions_return or []
    )
    ctx.long_term_store.get_session_history = AsyncMock(
        return_value=get_session_history_return or []
    )

    ctx.file_store = MagicMock()
    ctx.req = req
    ctx.stream_res = "relais:memory:response"
    return ctx


def _two_sessions() -> list[dict]:
    """Return two sample session dicts as returned by list_sessions()."""
    return [
        {
            "session_id": "sess_abc123",
            "last_active": 1745280000.0,  # 2026-04-22
            "turn_count": 3,
            "preview": "Bonjour, comment puis-je vous aider ?",
        },
        {
            "session_id": "sess_def456",
            "last_active": 1745193600.0,  # 2026-04-21
            "turn_count": 7,
            "preview": "Aide moi avec mon projet Python sil te plait",
        },
    ]


# ---------------------------------------------------------------------------
# SessionsHandler tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sessions_handler_formats_list():
    """list_sessions returns 2 sessions → formatted text contains expected markers."""
    from souvenir.handlers.sessions_handler import SessionsHandler

    orig = _make_orig_envelope()
    req = {
        "user_id": "usr_admin",
        "envelope_json": orig.to_json(),
    }
    ctx = _make_ctx(req=req, list_sessions_return=_two_sessions())

    handler = SessionsHandler()
    await handler.handle(ctx)

    # Capture the published payload
    assert ctx.redis_conn.xadd.called
    call_args = ctx.redis_conn.xadd.call_args
    payload_json = call_args[0][1]["payload"]
    published = Envelope.from_json(payload_json)

    assert "Sessions disponibles" in published.content
    assert "3 tours" in published.content or "3 turn" in published.content
    assert "7 tours" in published.content or "7 turn" in published.content
    assert "/resume" in published.content


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sessions_handler_empty():
    """list_sessions returns [] → text 'Aucune session trouvee.'"""
    from souvenir.handlers.sessions_handler import SessionsHandler

    orig = _make_orig_envelope()
    req = {
        "user_id": "usr_admin",
        "envelope_json": orig.to_json(),
    }
    ctx = _make_ctx(req=req, list_sessions_return=[])

    handler = SessionsHandler()
    await handler.handle(ctx)

    assert ctx.redis_conn.xadd.called
    call_args = ctx.redis_conn.xadd.call_args
    payload_json = call_args[0][1]["payload"]
    published = Envelope.from_json(payload_json)

    assert "Aucune session" in published.content


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sessions_handler_publishes_to_channel():
    """xadd is called on relais:messages:outgoing:{channel}."""
    from souvenir.handlers.sessions_handler import SessionsHandler

    orig = _make_orig_envelope(channel="discord")
    req = {
        "user_id": "usr_admin",
        "envelope_json": orig.to_json(),
    }
    ctx = _make_ctx(req=req, list_sessions_return=_two_sessions())

    handler = SessionsHandler()
    await handler.handle(ctx)

    assert ctx.redis_conn.xadd.called
    stream_name = ctx.redis_conn.xadd.call_args[0][0]
    assert stream_name == "relais:messages:outgoing:discord"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sessions_handler_correct_action():
    """Published envelope must have action == ACTION_MESSAGE_OUTGOING."""
    from souvenir.handlers.sessions_handler import SessionsHandler

    orig = _make_orig_envelope()
    req = {
        "user_id": "usr_admin",
        "envelope_json": orig.to_json(),
    }
    ctx = _make_ctx(req=req, list_sessions_return=_two_sessions())

    handler = SessionsHandler()
    await handler.handle(ctx)

    call_args = ctx.redis_conn.xadd.call_args
    payload_json = call_args[0][1]["payload"]
    published = Envelope.from_json(payload_json)

    assert published.action == ACTION_MESSAGE_OUTGOING


# ---------------------------------------------------------------------------
# ResumeHandler tests
# ---------------------------------------------------------------------------


def _one_turn() -> list[dict]:
    """Return a single archived turn for get_session_history()."""
    return [
        {
            "user_content": "Hello",
            "assistant_content": "Hi there!",
            "created_at": 1745280000.0,
            "correlation_id": "corr_abc",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_resume_handler_session_found():
    """get_session_history returns 1 turn → confirmation text contains 'reprise.'"""
    from souvenir.handlers.resume_handler import ResumeHandler

    orig = _make_orig_envelope()
    target_session = "sess_abc123"
    req = {
        "user_id": "usr_admin",
        "target_session_id": target_session,
        "envelope_json": orig.to_json(),
    }
    ctx = _make_ctx(req=req, get_session_history_return=_one_turn())

    handler = ResumeHandler()
    await handler.handle(ctx)

    assert ctx.redis_conn.xadd.called
    call_args = ctx.redis_conn.xadd.call_args
    payload_json = call_args[0][1]["payload"]
    published = Envelope.from_json(payload_json)

    assert "reprise" in published.content.lower()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_resume_handler_session_not_found():
    """get_session_history returns [] → text 'Session introuvable.'"""
    from souvenir.handlers.resume_handler import ResumeHandler

    orig = _make_orig_envelope()
    req = {
        "user_id": "usr_admin",
        "target_session_id": "sess_nonexistent",
        "envelope_json": orig.to_json(),
    }
    ctx = _make_ctx(req=req, get_session_history_return=[])

    handler = ResumeHandler()
    await handler.handle(ctx)

    assert ctx.redis_conn.xadd.called
    call_args = ctx.redis_conn.xadd.call_args
    payload_json = call_args[0][1]["payload"]
    published = Envelope.from_json(payload_json)

    assert "introuvable" in published.content.lower()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_resume_handler_includes_session_id_in_context():
    """When session is found, response.context['resume']['session_id'] == target_session_id."""
    from souvenir.handlers.resume_handler import ResumeHandler

    orig = _make_orig_envelope()
    target_session = "sess_abc123"
    req = {
        "user_id": "usr_admin",
        "target_session_id": target_session,
        "envelope_json": orig.to_json(),
    }
    ctx = _make_ctx(req=req, get_session_history_return=_one_turn())

    handler = ResumeHandler()
    await handler.handle(ctx)

    call_args = ctx.redis_conn.xadd.call_args
    payload_json = call_args[0][1]["payload"]
    published = Envelope.from_json(payload_json)

    assert "resume" in published.context
    assert published.context["resume"]["session_id"] == target_session


@pytest.mark.asyncio
@pytest.mark.unit
async def test_resume_handler_publishes_to_channel():
    """xadd is called on relais:messages:outgoing:{channel}."""
    from souvenir.handlers.resume_handler import ResumeHandler

    orig = _make_orig_envelope(channel="telegram")
    req = {
        "user_id": "usr_admin",
        "target_session_id": "sess_abc123",
        "envelope_json": orig.to_json(),
    }
    ctx = _make_ctx(req=req, get_session_history_return=_one_turn())

    handler = ResumeHandler()
    await handler.handle(ctx)

    assert ctx.redis_conn.xadd.called
    stream_name = ctx.redis_conn.xadd.call_args[0][0]
    assert stream_name == "relais:messages:outgoing:telegram"
