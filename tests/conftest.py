"""Shared pytest fixtures and helpers for the RELAIS test suite."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_INCOMING


def _make_envelope(
    content: str = "Hello world",
    channel: str = "discord",
    context: dict | None = None,
) -> Envelope:
    """Create a minimal Envelope for testing.

    Args:
        content: The message text.
        channel: The originating channel.
        context: Optional context dict (defaults to empty).

    Returns:
        A test Envelope instance.
    """
    return Envelope(
        content=content,
        sender_id="discord:123456789",
        channel=channel,
        session_id="sess-abc",
        correlation_id="corr-test-001",
        context=context or {},
        action=ACTION_MESSAGE_INCOMING,
    )


def _make_redis_mock() -> AsyncMock:
    """Create a fully mocked Redis connection.

    Returns:
        AsyncMock configured to behave as a Redis async client.
    """
    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xadd = AsyncMock(return_value="0-0")
    redis_conn.xack = AsyncMock()
    redis_conn.xread = AsyncMock(return_value=None)
    redis_conn.publish = AsyncMock()
    return redis_conn


def _make_xreadgroup_result(envelope: Envelope) -> list:
    """Wrap an envelope in the structure returned by xreadgroup.

    Args:
        envelope: The Envelope to embed as the stream message payload.

    Returns:
        List mimicking Redis xreadgroup output format.
    """
    return [("relais:tasks", [("1234567890-0", {"payload": envelope.to_json()})])]


def _default_profile_mock() -> MagicMock:
    """Return a MagicMock that behaves like a ProfileConfig.

    Returns:
        MagicMock with model, max_turns, max_turn_seconds and shell_timeout_seconds set.
    """
    m = MagicMock()
    m.model = "claude-opus-4-5"
    m.max_turns = 10
    m.max_turn_seconds = 300
    m.shell_timeout_seconds = 30
    return m


def _make_atelier() -> object:
    """Instantiate Atelier with filesystem-hitting loaders patched out.

    Returns:
        Atelier instance ready for use in tests.
    """
    from atelier.main import Atelier

    profile_mock = _default_profile_mock()
    mock_saver = MagicMock()
    mock_saver_cls = MagicMock()
    mock_saver_cls.from_conn_string.return_value = mock_saver

    patches = {
        "atelier.main.load_profiles": {"default": profile_mock},
        "atelier.main.load_for_sdk": {},
        "atelier.main.resolve_profile": profile_mock,
    }
    active = {}
    for target, retval in patches.items():
        p = patch(target, return_value=retval)
        active[target] = p
        p.start()

    p = patch("atelier.main.AsyncSqliteSaver", new=mock_saver_cls)
    active["saver"] = p
    p.start()

    try:
        atelier = Atelier()
    finally:
        for p_obj in active.values():
            try:
                p_obj.stop()
            except RuntimeError:
                pass

    return atelier


@contextmanager
def isolated_search_path(
    config_root: Path,
    native_root: Path | None = None,
) -> Generator[None, None, None]:
    """Patch both module-level path constants so no real on-disk pack leaks in.

    Used by subagent registry tests to restrict the config cascade to a
    temporary directory, preventing real user or project subagent packs
    from interfering with test assertions.

    Args:
        config_root: Replacement for ``CONFIG_SEARCH_PATH`` (single-element list).
        native_root: Replacement for ``NATIVE_SUBAGENTS_PATH``.  Defaults to a
            non-existent sub-directory so the native tier contributes nothing.

    Yields:
        None — just enters the patched context.

    Example:
        >>> with isolated_search_path(tmp_path) as _:
        ...     registry = SubagentRegistry.load()
        ...     assert registry.all_names == set()
    """
    if native_root is None:
        native_root = config_root / "_nonexistent_native_subagents_"
    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [config_root]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", native_root),
        patch(
            "atelier.subagents.resolve_bundles_dir",
            return_value=config_root / "_nonexistent_bundles_",
        ),
    ):
        yield


def make_mock_tool(name: str) -> MagicMock:
    """Return a MagicMock that duck-types as a BaseTool.

    Args:
        name: Tool name attribute.

    Returns:
        MagicMock with .name and .run set.
    """
    from langchain_core.tools import BaseTool
    m = MagicMock(spec=BaseTool)
    m.name = name
    return m


def make_fake_tool_registry(tools: dict | None = None) -> MagicMock:
    """Return a mock ToolRegistry.

    Args:
        tools: Dict mapping name -> BaseTool mock.

    Returns:
        MagicMock behaving like ToolRegistry.
    """
    registry = MagicMock()
    registry.get = lambda name: (tools or {}).get(name)
    registry.all = lambda: dict(tools or {})
    return registry


def write_pack(base_dir: Path, name: str, extra: dict | None = None) -> Path:
    """Create a minimal subagent pack directory.

    Args:
        base_dir: Parent directory (``config/atelier/subagents/``).
        name: Subagent name and directory name.
        extra: Extra YAML fields to merge.

    Returns:
        Path to the created ``subagent.yaml``.
    """
    pack_dir = base_dir / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {
        "name": name,
        "description": f"Description of {name}",
        "system_prompt": f"You are {name}.",
    }
    if extra:
        data.update(extra)
    yaml_path = pack_dir / "subagent.yaml"
    yaml_path.write_text(yaml.dump(data))
    return yaml_path
