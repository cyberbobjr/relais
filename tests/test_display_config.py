"""Unit tests for atelier.display_config.

Tests:
1. load_display_config() returns defaults when atelier.yaml is absent
2. load_display_config() reads enabled: false from a YAML file
3. Individual events are configurable (tool_call: false)
4. DisplayConfig is frozen (immutable)
5. detail_max_length is read from YAML
6. final_only is read from YAML
7. thinking event is read from YAML
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1. load_display_config() returns defaults when atelier.yaml absent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_display_config_returns_defaults_when_file_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_display_config() returns default DisplayConfig when atelier.yaml is missing.

    resolve_config_path is monkeypatched to raise FileNotFoundError so that
    the test is filesystem-independent.

    Args:
        monkeypatch: Pytest fixture for safe attribute patching.
    """
    import atelier.display_config as _mod

    monkeypatch.setattr(
        _mod,
        "resolve_config_path",
        lambda _: (_ for _ in ()).throw(FileNotFoundError("atelier.yaml not found")),
    )

    from atelier.display_config import load_display_config, DisplayConfig

    result = load_display_config()

    assert isinstance(result, DisplayConfig)
    assert result.enabled is True
    assert result.final_only is True
    assert result.detail_max_length == 100
    assert result.events == {
        "tool_call": True,
        "tool_result": True,
        "subagent_start": True,
        "thinking": False,
    }


# ---------------------------------------------------------------------------
# 2. load_display_config() reads enabled: false from YAML
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_display_config_reads_enabled_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_display_config() reads enabled=false from a temporary YAML file.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest fixture for safe attribute patching.
    """
    atelier_yaml = tmp_path / "atelier.yaml"
    atelier_yaml.write_text(
        "display:\n"
        "  enabled: false\n"
        "  final_only: true\n"
        "  detail_max_length: 100\n"
        "  events:\n"
        "    tool_call: true\n"
        "    tool_result: true\n"
        "    subagent_start: true\n"
        "    thinking: false\n"
    )

    import atelier.display_config as _mod

    monkeypatch.setattr(_mod, "resolve_config_path", lambda _: atelier_yaml)

    from atelier.display_config import load_display_config

    result = load_display_config()

    assert result.enabled is False


# ---------------------------------------------------------------------------
# 3. Individual events are configurable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_display_config_individual_event_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Individual events can be disabled via YAML (tool_call: false).

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest fixture for safe attribute patching.
    """
    atelier_yaml = tmp_path / "atelier.yaml"
    atelier_yaml.write_text(
        "display:\n"
        "  enabled: true\n"
        "  final_only: true\n"
        "  detail_max_length: 100\n"
        "  events:\n"
        "    tool_call: false\n"
        "    tool_result: true\n"
        "    subagent_start: true\n"
        "    thinking: false\n"
    )

    import atelier.display_config as _mod

    monkeypatch.setattr(_mod, "resolve_config_path", lambda _: atelier_yaml)

    from atelier.display_config import load_display_config

    result = load_display_config()

    assert result.events["tool_call"] is False
    assert result.events["tool_result"] is True
    assert result.events["subagent_start"] is True
    assert result.events["thinking"] is False


# ---------------------------------------------------------------------------
# 4. DisplayConfig is frozen (immutable)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_display_config_is_frozen() -> None:
    """DisplayConfig instances are immutable; attribute assignment raises FrozenInstanceError."""
    from atelier.display_config import DisplayConfig

    config = DisplayConfig()

    with pytest.raises(FrozenInstanceError):
        config.enabled = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 5. detail_max_length is read from YAML
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_display_config_reads_detail_max_length(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """detail_max_length is loaded from YAML when specified.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest fixture for safe attribute patching.
    """
    atelier_yaml = tmp_path / "atelier.yaml"
    atelier_yaml.write_text(
        "display:\n"
        "  enabled: true\n"
        "  final_only: true\n"
        "  detail_max_length: 42\n"
        "  events:\n"
        "    tool_call: true\n"
        "    tool_result: true\n"
        "    subagent_start: true\n"
        "    thinking: false\n"
    )

    import atelier.display_config as _mod

    monkeypatch.setattr(_mod, "resolve_config_path", lambda _: atelier_yaml)

    from atelier.display_config import load_display_config

    result = load_display_config()

    assert result.detail_max_length == 42


# ---------------------------------------------------------------------------
# 6. final_only is read from YAML
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_display_config_reads_final_only_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """final_only=false is loaded from YAML when specified.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest fixture for safe attribute patching.
    """
    atelier_yaml = tmp_path / "atelier.yaml"
    atelier_yaml.write_text(
        "display:\n"
        "  enabled: true\n"
        "  final_only: false\n"
        "  detail_max_length: 100\n"
        "  events:\n"
        "    tool_call: true\n"
        "    tool_result: true\n"
        "    subagent_start: true\n"
        "    thinking: false\n"
    )

    import atelier.display_config as _mod

    monkeypatch.setattr(_mod, "resolve_config_path", lambda _: atelier_yaml)

    from atelier.display_config import load_display_config

    result = load_display_config()

    assert result.final_only is False


# ---------------------------------------------------------------------------
# 7. thinking event is read from YAML
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_display_config_reads_thinking_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """thinking event can be enabled via YAML.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest fixture for safe attribute patching.
    """
    atelier_yaml = tmp_path / "atelier.yaml"
    atelier_yaml.write_text(
        "display:\n"
        "  enabled: true\n"
        "  final_only: true\n"
        "  detail_max_length: 100\n"
        "  events:\n"
        "    tool_call: true\n"
        "    tool_result: true\n"
        "    subagent_start: true\n"
        "    thinking: true\n"
    )

    import atelier.display_config as _mod

    monkeypatch.setattr(_mod, "resolve_config_path", lambda _: atelier_yaml)

    from atelier.display_config import load_display_config

    result = load_display_config()

    assert result.events["thinking"] is True


# ---------------------------------------------------------------------------
# 8. Partial events override — unspecified events keep their defaults
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_display_config_partial_events_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unspecified events inherit their default value when only some events are overridden.

    If YAML specifies only ``tool_call: false``, the other events (tool_result,
    subagent_start, thinking) must retain their default values from
    ``_DEFAULT_EVENTS``.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest fixture for safe attribute patching.
    """
    atelier_yaml = tmp_path / "atelier.yaml"
    atelier_yaml.write_text(
        "display:\n"
        "  events:\n"
        "    tool_call: false\n"
    )

    import atelier.display_config as _mod

    monkeypatch.setattr(_mod, "resolve_config_path", lambda _: atelier_yaml)

    from atelier.display_config import load_display_config

    result = load_display_config()

    assert result.events["tool_call"] is False
    # Unspecified events must keep their _DEFAULT_EVENTS values
    assert result.events["tool_result"] is True
    assert result.events["subagent_start"] is True
    assert result.events["thinking"] is False


# ---------------------------------------------------------------------------
# 9. Invalid YAML value — returns defaults with WARNING log
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_display_config_invalid_yaml_value_returns_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """load_display_config() falls back to defaults and logs WARNING on invalid YAML types.

    A non-integer ``detail_max_length`` triggers the ``except (TypeError, ValueError)``
    branch inside ``load_display_config()``.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest fixture for safe attribute patching.
        caplog: Pytest log capture fixture.
    """
    import logging

    atelier_yaml = tmp_path / "atelier.yaml"
    atelier_yaml.write_text(
        "display:\n"
        "  detail_max_length: not_a_number\n"
    )

    import atelier.display_config as _mod

    monkeypatch.setattr(_mod, "resolve_config_path", lambda _: atelier_yaml)

    from atelier.display_config import load_display_config, DisplayConfig

    with caplog.at_level(logging.WARNING, logger="atelier.display_config"):
        result = load_display_config()

    assert isinstance(result, DisplayConfig)
    assert result.enabled is True
    assert result.detail_max_length == 100
    assert any("invalid" in r.message.lower() for r in caplog.records)
    # The warning must name the specific field
    assert any("detail_max_length" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 10. Per-field validation — bad field falls back; valid fields are applied
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_display_config_per_field_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Invalid field falls back to default while sibling valid fields are applied.

    A non-bool ``enabled`` triggers a WARNING for that field only; the valid
    ``detail_max_length: 42`` is still applied correctly.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest fixture for safe attribute patching.
        caplog: Pytest log capture fixture.
    """
    import logging

    atelier_yaml = tmp_path / "atelier.yaml"
    atelier_yaml.write_text(
        "display:\n"
        "  enabled: not_a_bool\n"
        "  detail_max_length: 42\n"
    )

    import atelier.display_config as _mod

    monkeypatch.setattr(_mod, "resolve_config_path", lambda _: atelier_yaml)

    from atelier.display_config import load_display_config

    with caplog.at_level(logging.WARNING, logger="atelier.display_config"):
        result = load_display_config()

    # Invalid field falls back to its default
    assert result.enabled is True
    # Valid sibling field is still applied
    assert result.detail_max_length == 42
    # Warning names the specific invalid field
    assert any("enabled" in r.message for r in caplog.records)
