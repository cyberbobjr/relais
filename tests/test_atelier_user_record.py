"""TDD RED tests — Atelier reads user context from user_record dict in envelope.metadata.

Previously Atelier read individual top-level metadata keys:
  - user_role  → envelope.metadata.get("user_role")
  - llm_profile → envelope.metadata.get("llm_profile")
  - skills_dirs → envelope.metadata.get("skills_dirs")
  - allowed_mcp_tools → envelope.metadata.get("allowed_mcp_tools")
  - custom_prompt_path → envelope.metadata.get("custom_prompt_path")

After the Portail refactor, all user identity is consolidated under:
  envelope.metadata["user_record"] = {
      "role": ...,
      "llm_profile": ...,
      "skills_dirs": [...],
      "allowed_mcp_tools": [...],
      "prompt_path": ...,
      ...
  }

These tests verify that Atelier reads from user_record exclusively.

RED phase: written BEFORE the implementation change. They MUST FAIL initially.
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
async def test_atelier_reads_llm_profile_from_user_record() -> None:
    """Atelier reads llm_profile from user_record dict, not top-level metadata.

    The envelope has no top-level 'llm_profile' key, only user_record.llm_profile.
    Atelier must forward the correct profile name to resolve_profile().
    """
    atelier = _make_atelier()
    envelope = _make_envelope(metadata={
        "user_record": {
            "role": "user",
            "llm_profile": "fast",
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
async def test_atelier_reads_role_from_user_record_for_soul_assembly() -> None:
    """Atelier reads role from user_record and passes it as user_role to assemble_system_prompt.

    The envelope has no top-level 'user_role' key; role is inside user_record.
    """
    atelier = _make_atelier()
    envelope = _make_envelope(metadata={
        "user_record": {
            "role": "admin",
            "llm_profile": "default",
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
    assert call_kwargs.get("user_role") == "admin", (
        f"Expected user_role='admin' from user_record.role, got {call_kwargs.get('user_role')!r}"
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
        "user_record": {
            "role": "user",
            "llm_profile": "default",
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
        "user_record": {
            "role": "user",
            "llm_profile": "default",
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
# T5 — role=None when user_record absent
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_user_role_none_when_user_record_absent() -> None:
    """Atelier passes user_role=None when envelope has no user_record.

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
    assert call_kwargs.get("user_role") is None, (
        f"Expected user_role=None when user_record absent, got {call_kwargs.get('user_role')!r}"
    )
