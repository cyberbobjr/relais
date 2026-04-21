"""Unit tests for atelier.prompts — TDD RED first.

Tests validate:
- Constants are importable from atelier.prompts
- build_project_context_prompt produces expected anchors
- _build_execution_context renders envelope metadata correctly
- _enrich_system_prompt appends mandatory sections without duplication
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_prompts_constants_importable() -> None:
    """All prompt constants must be importable from atelier.prompts."""
    from atelier.prompts import (  # noqa: F401
        LONG_TERM_MEMORY_PROMPT,
        SELF_DIAGNOSIS_PROMPT,
        DIAGNOSTIC_AWARENESS_PROMPT,
        DIAGNOSTIC_MARKER,
    )


@pytest.mark.unit
def test_diagnostic_marker_value() -> None:
    """DIAGNOSTIC_MARKER must equal the expected string."""
    from atelier.prompts import DIAGNOSTIC_MARKER

    assert DIAGNOSTIC_MARKER == "[DIAGNOSTIC — internal]"


@pytest.mark.unit
def test_long_term_memory_prompt_nonempty() -> None:
    """LONG_TERM_MEMORY_PROMPT must be a non-empty string."""
    from atelier.prompts import LONG_TERM_MEMORY_PROMPT

    assert isinstance(LONG_TERM_MEMORY_PROMPT, str)
    assert len(LONG_TERM_MEMORY_PROMPT) > 0


@pytest.mark.unit
def test_self_diagnosis_prompt_nonempty() -> None:
    """SELF_DIAGNOSIS_PROMPT must be a non-empty string."""
    from atelier.prompts import SELF_DIAGNOSIS_PROMPT

    assert isinstance(SELF_DIAGNOSIS_PROMPT, str)
    assert len(SELF_DIAGNOSIS_PROMPT) > 0


@pytest.mark.unit
def test_diagnostic_awareness_prompt_contains_marker() -> None:
    """DIAGNOSTIC_AWARENESS_PROMPT must reference DIAGNOSTIC_MARKER."""
    from atelier.prompts import DIAGNOSTIC_AWARENESS_PROMPT, DIAGNOSTIC_MARKER

    assert DIAGNOSTIC_MARKER in DIAGNOSTIC_AWARENESS_PROMPT


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
# _enrich_system_prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_system_prompt_appends_long_term_memory() -> None:
    """_enrich_system_prompt appends LONG_TERM_MEMORY_PROMPT when absent."""
    from atelier.prompts import _enrich_system_prompt, LONG_TERM_MEMORY_PROMPT

    result = _enrich_system_prompt("You are helpful.")
    assert LONG_TERM_MEMORY_PROMPT in result


@pytest.mark.unit
def test_enrich_system_prompt_no_duplicate_long_term_memory() -> None:
    """_enrich_system_prompt does not duplicate LONG_TERM_MEMORY_PROMPT if present."""
    from atelier.prompts import _enrich_system_prompt, LONG_TERM_MEMORY_PROMPT

    base = f"You are helpful.\n\n{LONG_TERM_MEMORY_PROMPT}"
    result = _enrich_system_prompt(base)
    assert result.count(LONG_TERM_MEMORY_PROMPT) == 1


@pytest.mark.unit
def test_enrich_system_prompt_appends_self_diagnosis() -> None:
    """_enrich_system_prompt appends SELF_DIAGNOSIS_PROMPT when absent."""
    from atelier.prompts import _enrich_system_prompt, SELF_DIAGNOSIS_PROMPT

    result = _enrich_system_prompt("base")
    assert SELF_DIAGNOSIS_PROMPT in result


@pytest.mark.unit
def test_enrich_system_prompt_appends_delegation_prompt() -> None:
    """_enrich_system_prompt appends delegation_prompt when provided."""
    from atelier.prompts import _enrich_system_prompt

    result = _enrich_system_prompt("base", delegation_prompt="Delegate to mail-agent.")
    assert "Delegate to mail-agent." in result


@pytest.mark.unit
def test_enrich_system_prompt_no_delegation_when_empty() -> None:
    """_enrich_system_prompt does not append anything extra when delegation_prompt is empty."""
    from atelier.prompts import _enrich_system_prompt

    result = _enrich_system_prompt("base", delegation_prompt="")
    # Delegation section should not be an extra blank section
    assert "Delegate" not in result


@pytest.mark.unit
def test_enrich_system_prompt_appends_project_context() -> None:
    """_enrich_system_prompt appends project_context when non-empty."""
    from atelier.prompts import _enrich_system_prompt

    result = _enrich_system_prompt("base", project_context="RELAIS_HOME=/foo")
    assert "RELAIS_HOME=/foo" in result


@pytest.mark.unit
def test_enrich_system_prompt_skips_empty_project_context() -> None:
    """_enrich_system_prompt does not inject empty project_context."""
    from atelier.prompts import _enrich_system_prompt

    result = _enrich_system_prompt("base", project_context="")
    # Should not add a blank section for project_context
    assert result.strip() != ""
