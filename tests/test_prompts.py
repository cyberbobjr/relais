"""Unit tests for atelier.prompts.

Tests validate:
- DIAGNOSTIC_MARKER constant is importable
- build_project_context_prompt produces expected anchors
- _build_execution_context renders envelope metadata correctly
- _build_core_system_prompt reads SYSTEM_PROMPT.md and appends dynamic sections
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_diagnostic_marker_importable() -> None:
    """DIAGNOSTIC_MARKER must be importable from atelier.prompts."""
    from atelier.prompts import DIAGNOSTIC_MARKER  # noqa: F401


@pytest.mark.unit
def test_diagnostic_marker_value() -> None:
    """DIAGNOSTIC_MARKER must equal the expected string."""
    from atelier.prompts import DIAGNOSTIC_MARKER

    assert DIAGNOSTIC_MARKER == "[DIAGNOSTIC — internal]"


# ---------------------------------------------------------------------------
# build_project_context_prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_project_context_prompt_contains_relais_home() -> None:
    """build_project_context_prompt must include the RELAIS_HOME path."""
    from atelier.prompts import build_project_context_prompt

    result = build_project_context_prompt("/tmp/relais_home", "/tmp/project")
    assert "/tmp/relais_home" in result


@pytest.mark.unit
def test_build_project_context_prompt_contains_project_dir() -> None:
    """build_project_context_prompt must include the project dir path."""
    from atelier.prompts import build_project_context_prompt

    result = build_project_context_prompt("/tmp/relais_home", "/tmp/project")
    assert "/tmp/project" in result


@pytest.mark.unit
def test_build_project_context_prompt_contains_tui_dir() -> None:
    """build_project_context_prompt must include the TUI directory path."""
    from atelier.prompts import build_project_context_prompt

    result = build_project_context_prompt("/home/user/.relais", "/src/relais")
    assert "tools/tui-ts" in result


@pytest.mark.unit
def test_build_project_context_prompt_never_search_from_root() -> None:
    """build_project_context_prompt must warn against searching from /."""
    from atelier.prompts import build_project_context_prompt

    result = build_project_context_prompt("/a", "/b")
    assert "Never run" in result or "NEVER" in result or "never" in result.lower()


# ---------------------------------------------------------------------------
# _build_execution_context
# ---------------------------------------------------------------------------


def _make_envelope(
    sender_id: str = "discord:123",
    channel: str = "discord",
    session_id: str = "sess-1",
    correlation_id: str = "corr-1",
    reply_to: str = "",
) -> MagicMock:
    """Return a minimal mock Envelope."""
    env = MagicMock()
    env.sender_id = sender_id
    env.channel = channel
    env.session_id = session_id
    env.correlation_id = correlation_id
    env.context = {
        "aiguilleur": {"reply_to": reply_to} if reply_to else {},
        "portail": {},
    }
    return env


@pytest.mark.unit
def test_build_execution_context_contains_sender_id() -> None:
    """_build_execution_context must include envelope.sender_id."""
    from atelier.prompts import _build_execution_context

    env = _make_envelope(sender_id="telegram:456")
    result = _build_execution_context(env)
    assert "telegram:456" in result


@pytest.mark.unit
def test_build_execution_context_contains_channel() -> None:
    """_build_execution_context must include envelope.channel."""
    from atelier.prompts import _build_execution_context

    env = _make_envelope(channel="telegram")
    result = _build_execution_context(env)
    assert "telegram" in result


@pytest.mark.unit
def test_build_execution_context_contains_session_id() -> None:
    """_build_execution_context must include envelope.session_id."""
    from atelier.prompts import _build_execution_context

    env = _make_envelope(session_id="my-session-42")
    result = _build_execution_context(env)
    assert "my-session-42" in result


@pytest.mark.unit
def test_build_execution_context_contains_correlation_id() -> None:
    """_build_execution_context must include envelope.correlation_id."""
    from atelier.prompts import _build_execution_context

    env = _make_envelope(correlation_id="uuid-999")
    result = _build_execution_context(env)
    assert "uuid-999" in result


@pytest.mark.unit
def test_build_execution_context_contains_reply_to() -> None:
    """_build_execution_context must include reply_to from aiguilleur context."""
    from atelier.prompts import _build_execution_context

    env = _make_envelope(reply_to="whatsapp")
    result = _build_execution_context(env)
    assert "whatsapp" in result


@pytest.mark.unit
def test_build_execution_context_wrapped_in_tags() -> None:
    """_build_execution_context output must be wrapped in relais_execution_context tags."""
    from atelier.prompts import _build_execution_context

    env = _make_envelope()
    result = _build_execution_context(env)
    assert "<relais_execution_context>" in result
    assert "</relais_execution_context>" in result


# ---------------------------------------------------------------------------
# _build_core_system_prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_core_system_prompt_is_nonempty() -> None:
    """_build_core_system_prompt returns a non-empty string from SYSTEM_PROMPT.md."""
    from atelier.prompts import _build_core_system_prompt

    result = _build_core_system_prompt()
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.unit
def test_build_core_system_prompt_contains_diagnostic_marker() -> None:
    """_build_core_system_prompt includes DIAGNOSTIC_MARKER from SYSTEM_PROMPT.md."""
    from atelier.prompts import _build_core_system_prompt, DIAGNOSTIC_MARKER

    result = _build_core_system_prompt()
    assert DIAGNOSTIC_MARKER in result


@pytest.mark.unit
def test_build_core_system_prompt_appends_delegation_prompt() -> None:
    """_build_core_system_prompt appends delegation_prompt when provided."""
    from atelier.prompts import _build_core_system_prompt

    result = _build_core_system_prompt(delegation_prompt="Delegate to mail-agent.")
    assert "Delegate to mail-agent." in result


@pytest.mark.unit
def test_build_core_system_prompt_no_delegation_when_empty() -> None:
    """_build_core_system_prompt does not append extra text when delegation_prompt is empty."""
    from atelier.prompts import _build_core_system_prompt

    result_without = _build_core_system_prompt(delegation_prompt="")
    result_with = _build_core_system_prompt(delegation_prompt="Delegate.")
    assert "Delegate." not in result_without
    assert "Delegate." in result_with


@pytest.mark.unit
def test_build_core_system_prompt_appends_project_context() -> None:
    """_build_core_system_prompt appends project_context when non-empty."""
    from atelier.prompts import _build_core_system_prompt

    result = _build_core_system_prompt(project_context="RELAIS_HOME=/foo")
    assert "RELAIS_HOME=/foo" in result


@pytest.mark.unit
def test_build_core_system_prompt_skips_empty_project_context() -> None:
    """_build_core_system_prompt does not inject empty project_context."""
    from atelier.prompts import _build_core_system_prompt

    result_without = _build_core_system_prompt(project_context="")
    result_with = _build_core_system_prompt(project_context="RELAIS_HOME=/bar")
    assert "RELAIS_HOME=/bar" not in result_without
    assert "RELAIS_HOME=/bar" in result_with
