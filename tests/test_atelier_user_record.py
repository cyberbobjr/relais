"""Tests — Atelier reads user context from user_record dict and top-level metadata.

User identity fields (role, skills_dirs, allowed_mcp_tools, prompt_path) are
inside ``envelope.metadata["user_record"]``.

``llm_profile`` is stamped directly into ``envelope.metadata["llm_profile"]``
by Portail (not inside user_record).

These tests verify that Atelier reads each field from the correct location.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.envelope import Envelope
from atelier.agent_executor import AgentExecutionError


# ---------------------------------------------------------------------------
# Shared helpers (same pattern as test_atelier.py)
# ---------------------------------------------------------------------------


def _make_envelope(
    content: str = "Hello",
    channel: str = "discord",
    metadata: dict | None = None,
) -> Envelope:
    """Build a minimal Envelope for atelier user_record tests.

    Args:
        content: Message text.
        channel: Originating channel.
        metadata: Optional metadata dict.

    Returns:
        A test Envelope instance.
    """
    return Envelope(
        content=content,
        sender_id="discord:123456789",
        channel=channel,
        session_id="sess-abc",
        correlation_id="corr-ur-001",
        metadata=metadata or {},
    )


def _make_redis_mock() -> AsyncMock:
    """Create a fully mocked Redis connection.

    Returns:
        AsyncMock configured as a Redis async client.
    """
    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xadd = AsyncMock(return_value="0-0")
    redis_conn.xack = AsyncMock()
    redis_conn.xread = AsyncMock(return_value=None)
    redis_conn.publish = AsyncMock()
    return redis_conn


def _make_xreadgroup_result(envelope: Envelope) -> list:
    """Wrap an envelope in xreadgroup output format.

    Args:
        envelope: The Envelope to embed.

    Returns:
        List mimicking Redis xreadgroup output.
    """
    return [("relais:tasks", [("1234567890-0", {"payload": envelope.to_json()})])]


def _default_profile_mock() -> MagicMock:
    """Return a minimal ProfileConfig mock.

    Returns:
        MagicMock with model and max_turns set.
    """
    m = MagicMock()
    m.model = "claude-opus-4-5"
    m.max_turns = 10
    return m


def _make_atelier():
    """Instantiate Atelier with __init__-time loaders patched out.

    Returns:
        Atelier instance with filesystem loaders bypassed.
    """
    from atelier.main import Atelier

    with (
        patch("atelier.main.load_profiles", return_value={"default": _default_profile_mock()}),
        patch("atelier.main.load_for_sdk", return_value={}),
        patch("atelier.main.load_channels_config", return_value={}),
        patch("atelier.main.resolve_skills_dir", return_value=Path("/tmp")),
        patch("atelier.main.RedisClient"),
    ):
        return Atelier()


def _run_stream_once(atelier, envelope, extra_patches=None):
    """Helper to run _process_stream for exactly one message then cancel.

    Args:
        atelier: Atelier instance under test.
        envelope: Envelope to inject into the stream.
        extra_patches: Optional dict of additional patch targets.

    Returns:
        Coroutine that runs the stream loop.
    """
    redis_conn = _make_redis_mock()
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])
    return atelier, redis_conn


# ---------------------------------------------------------------------------
# T1 — llm_profile read from user_record
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_reads_llm_profile_from_top_level_metadata() -> None:
    """Atelier reads llm_profile from envelope.metadata['llm_profile'], not user_record.

    The envelope has llm_profile at the top level of metadata (stamped by Portail).
    Atelier must forward the correct profile name to resolve_profile().
    """
    atelier = _make_atelier()
    envelope = _make_envelope(metadata={
        "llm_profile": "fast",
        "user_record": {
            "role": "user",
            "prompt_path": None,
            "skills_dirs": [],
            "allowed_mcp_tools": [],
            "display_name": "Alice",
            "blocked": False,
            "actions": ["send"],
        }
    })
    redis_conn = _make_redis_mock()
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    resolve_profile_calls: list[str] = []

    def capture_resolve_profile(profiles, name):
        resolve_profile_calls.append(name)
        return _default_profile_mock()

    with (
        patch("atelier.main.AgentExecutor") as MockExec,
        patch("atelier.main.McpSessionManager"),
        patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("atelier.main.resolve_profile", side_effect=capture_resolve_profile),
        patch("atelier.main.assemble_system_prompt", return_value="soul"),
        patch("atelier.main.load_for_sdk", return_value={}),
    ):
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value="reply")
        MockExec.return_value = mock_instance

        try:
            await atelier._process_stream(redis_conn)
        except asyncio.CancelledError:
            pass

    assert resolve_profile_calls, "resolve_profile should have been called"
    assert resolve_profile_calls[0] == "fast", (
        f"Expected profile 'fast' from user_record, got {resolve_profile_calls[0]!r}"
    )


# ---------------------------------------------------------------------------
# T2 — role read from user_record and forwarded to assemble_system_prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_reads_role_prompt_path_from_user_record_for_soul_assembly() -> None:
    """Atelier reads role_prompt_path from user_record and forwards it to assemble_system_prompt.

    The role_prompt_path is passed as a keyword argument so the role overlay
    is included in the assembled system prompt.
    """
    atelier = _make_atelier()
    envelope = _make_envelope(metadata={
        "llm_profile": "default",
        "user_record": {
            "role": "admin",
            "role_prompt_path": "roles/admin.md",
            "prompt_path": None,
            "skills_dirs": [],
            "allowed_mcp_tools": [],
            "display_name": "Admin",
            "blocked": False,
            "actions": ["*"],
        }
    })
    redis_conn = _make_redis_mock()
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    mock_sp = MagicMock(return_value="soul")

    with (
        patch("atelier.main.AgentExecutor") as MockExec,
        patch("atelier.main.McpSessionManager"),
        patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("atelier.main.resolve_profile", return_value=_default_profile_mock()),
        patch("atelier.main.assemble_system_prompt", mock_sp),
        patch("atelier.main.load_for_sdk", return_value={}),
    ):
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value="reply")
        MockExec.return_value = mock_instance

        try:
            await atelier._process_stream(redis_conn)
        except asyncio.CancelledError:
            pass

    mock_sp.assert_called_once()
    call_kwargs = mock_sp.call_args.kwargs
    assert call_kwargs.get("role_prompt_path") == "roles/admin.md", (
        f"Expected role_prompt_path='roles/admin.md' from user_record, got {call_kwargs.get('role_prompt_path')!r}"
    )


# ---------------------------------------------------------------------------
# T3 — prompt_path read from user_record
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_reads_prompt_path_from_user_record() -> None:
    """Atelier reads prompt_path from user_record, not 'custom_prompt_path' top-level key.

    The field was renamed from custom_prompt_path to prompt_path inside user_record.
    """
    atelier = _make_atelier()
    envelope = _make_envelope(metadata={
        "llm_profile": "default",
        "user_record": {
            "role": "user",
            "prompt_path": "users/discord_123.md",
            "skills_dirs": [],
            "allowed_mcp_tools": [],
            "display_name": "Alice",
            "blocked": False,
            "actions": ["send"],
        }
    })
    redis_conn = _make_redis_mock()
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    mock_sp = MagicMock(return_value="soul")

    with (
        patch("atelier.main.AgentExecutor") as MockExec,
        patch("atelier.main.McpSessionManager"),
        patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("atelier.main.resolve_profile", return_value=_default_profile_mock()),
        patch("atelier.main.assemble_system_prompt", mock_sp),
        patch("atelier.main.load_for_sdk", return_value={}),
    ):
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value="reply")
        MockExec.return_value = mock_instance

        try:
            await atelier._process_stream(redis_conn)
        except asyncio.CancelledError:
            pass

    mock_sp.assert_called_once()
    call_kwargs = mock_sp.call_args.kwargs
    assert call_kwargs.get("user_prompt_path") == "users/discord_123.md", (
        f"Expected prompt_path from user_record, got {call_kwargs.get('user_prompt_path')!r}"
    )


# ---------------------------------------------------------------------------
# T4 — skills_dirs read from user_record
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_reads_skills_dirs_from_user_record(tmp_path: Path) -> None:
    """Atelier reads skills_dirs from user_record, not top-level metadata.

    The resolved skills paths must be passed to AgentExecutor.
    """
    (tmp_path / "coding").mkdir()
    atelier = _make_atelier()
    atelier._skills_base_dir = tmp_path

    envelope = _make_envelope(metadata={
        "llm_profile": "default",
        "user_record": {
            "role": "user",
            "prompt_path": None,
            "skills_dirs": ["coding"],
            "allowed_mcp_tools": [],
            "display_name": "Alice",
            "blocked": False,
            "actions": ["send"],
        }
    })
    redis_conn = _make_redis_mock()
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    executor_calls: list[dict] = []

    with patch("atelier.main.AgentExecutor") as MockExec:
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value="reply")

        def capture(**kwargs):
            executor_calls.append(kwargs)
            return mock_instance

        MockExec.side_effect = capture

        with (
            patch("atelier.main.McpSessionManager"),
            patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]),
            patch("atelier.main.resolve_profile", return_value=_default_profile_mock()),
            patch("atelier.main.assemble_system_prompt", return_value="soul"),
            patch("atelier.main.load_for_sdk", return_value={}),
        ):
            try:
                await atelier._process_stream(redis_conn)
            except asyncio.CancelledError:
                pass

    assert len(executor_calls) == 1
    assert "skills" in executor_calls[0]
    assert str(tmp_path / "coding") in executor_calls[0]["skills"], (
        f"Expected coding skill path in executor skills, got {executor_calls[0]['skills']}"
    )


# ---------------------------------------------------------------------------
# T5 — role_prompt_path=None when user_record absent
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_role_prompt_path_none_when_user_record_absent() -> None:
    """Atelier passes role_prompt_path=None when envelope has no user_record.

    Should not raise KeyError — just degrade gracefully.
    """
    atelier = _make_atelier()
    envelope = _make_envelope(metadata={})  # no user_record at all
    redis_conn = _make_redis_mock()
    redis_conn.xreadgroup = AsyncMock(side_effect=[
        _make_xreadgroup_result(envelope),
        asyncio.CancelledError(),
    ])

    mock_sp = MagicMock(return_value="soul")

    with (
        patch("atelier.main.AgentExecutor") as MockExec,
        patch("atelier.main.McpSessionManager"),
        patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("atelier.main.resolve_profile", return_value=_default_profile_mock()),
        patch("atelier.main.assemble_system_prompt", mock_sp),
        patch("atelier.main.load_for_sdk", return_value={}),
    ):
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(return_value="reply")
        MockExec.return_value = mock_instance

        try:
            await atelier._process_stream(redis_conn)
        except asyncio.CancelledError:
            pass

    mock_sp.assert_called_once()
    call_kwargs = mock_sp.call_args.kwargs
    assert call_kwargs.get("role_prompt_path") is None, (
        f"Expected role_prompt_path=None when user_record absent, got {call_kwargs.get('role_prompt_path')!r}"
    )
