"""Unit tests for atelier.progress_config — written TDD (RED first).

Tests:
1. load_progress_config() returns defaults when atelier.yaml is absent
2. load_progress_config() reads enabled: false from a YAML file
3. Individual events are configurable (tool_call: false)
4. ProgressConfig is frozen (immutable)
5. detail_max_length is read from YAML
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# 1. load_progress_config() returns defaults when atelier.yaml absent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_progress_config_returns_defaults_when_file_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_progress_config() returns default ProgressConfig when atelier.yaml is missing.

    resolve_config_path is monkeypatched to raise FileNotFoundError so that
    the test is filesystem-independent.

    Args:
        monkeypatch: Pytest fixture for safe attribute patching.
    """
    import atelier.progress_config as _mod

    monkeypatch.setattr(
        _mod,
        "resolve_config_path",
        lambda _: (_ for _ in ()).throw(FileNotFoundError("atelier.yaml not found")),
    )

    from atelier.progress_config import load_progress_config, ProgressConfig

    result = load_progress_config()

    assert isinstance(result, ProgressConfig)
    assert result.enabled is True
    assert result.publish_to_outgoing is True
    assert result.detail_max_length == 100
    assert result.events == {"tool_call": True, "tool_result": True, "subagent_start": True}


# ---------------------------------------------------------------------------
# 2. load_progress_config() reads enabled: false from YAML
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_progress_config_reads_enabled_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_progress_config() reads enabled=false from a temporary YAML file.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest fixture for safe attribute patching.
    """
    atelier_yaml = tmp_path / "atelier.yaml"
    atelier_yaml.write_text(
        "progress:\n"
        "  enabled: false\n"
        "  events:\n"
        "    tool_call: true\n"
        "    tool_result: true\n"
        "    subagent_start: true\n"
        "  publish_to_outgoing: true\n"
        "  detail_max_length: 100\n"
    )

    import atelier.progress_config as _mod

    monkeypatch.setattr(_mod, "resolve_config_path", lambda _: atelier_yaml)

    from atelier.progress_config import load_progress_config

    result = load_progress_config()

    assert result.enabled is False


# ---------------------------------------------------------------------------
# 3. Individual events are configurable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_progress_config_individual_event_disabled(
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
        "progress:\n"
        "  enabled: true\n"
        "  events:\n"
        "    tool_call: false\n"
        "    tool_result: true\n"
        "    subagent_start: true\n"
        "  publish_to_outgoing: true\n"
        "  detail_max_length: 100\n"
    )

    import atelier.progress_config as _mod

    monkeypatch.setattr(_mod, "resolve_config_path", lambda _: atelier_yaml)

    from atelier.progress_config import load_progress_config

    result = load_progress_config()

    assert result.events["tool_call"] is False
    assert result.events["tool_result"] is True
    assert result.events["subagent_start"] is True


# ---------------------------------------------------------------------------
# 4. ProgressConfig is frozen (immutable)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_progress_config_is_frozen() -> None:
    """ProgressConfig instances are immutable; attribute assignment raises FrozenInstanceError."""
    from atelier.progress_config import ProgressConfig

    config = ProgressConfig()

    with pytest.raises(FrozenInstanceError):
        config.enabled = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 5. detail_max_length is read from YAML
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_progress_config_reads_detail_max_length(
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
        "progress:\n"
        "  enabled: true\n"
        "  events:\n"
        "    tool_call: true\n"
        "    tool_result: true\n"
        "    subagent_start: true\n"
        "  publish_to_outgoing: false\n"
        "  detail_max_length: 42\n"
    )

    import atelier.progress_config as _mod

    monkeypatch.setattr(_mod, "resolve_config_path", lambda _: atelier_yaml)

    from atelier.progress_config import load_progress_config

    result = load_progress_config()

    assert result.detail_max_length == 42
    assert result.publish_to_outgoing is False
