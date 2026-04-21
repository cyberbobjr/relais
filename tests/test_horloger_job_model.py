"""Tests for horloger.job_model — written FIRST per TDD (RED phase).

Tests cover:
- Happy path: valid YAML loads into a JobSpec
- Missing required fields raise ValueError with clear messages
- Invalid cron expression raises ValueError
- Timezone defaults to "UTC" when absent from YAML
- The frozen dataclass cannot be mutated
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_yaml(tmp_path: Path, name: str, data: dict) -> Path:
    """Write *data* as YAML into *tmp_path/name* and return the path.

    Args:
        tmp_path: Temporary directory provided by pytest.
        name: File name (e.g. ``"job.yaml"``).
        data: Dictionary to serialise as YAML.

    Returns:
        Path to the written file.
    """
    p = tmp_path / name
    p.write_text(yaml.dump(data), encoding="utf-8")
    return p


VALID_DATA: dict = {
    "id": "weather-morning",
    "owner_id": "usr_alice",
    "schedule": "0 8 * * *",
    "channel": "discord",
    "prompt": "Donne-moi la météo de Lyon pour aujourd'hui",
    "enabled": True,
    "created_at": "2026-04-20T08:00:00Z",
    "description": "Météo matinale",
    "timezone": "Europe/Paris",
}


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_job_yaml_happy_path(tmp_path: Path) -> None:
    """Valid YAML with all fields loads into a JobSpec without error.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_model import JobSpec, load_job_yaml

    path = write_yaml(tmp_path, "weather-morning.yaml", VALID_DATA)
    spec = load_job_yaml(path)

    assert isinstance(spec, JobSpec)
    assert spec.id == "weather-morning"
    assert spec.owner_id == "usr_alice"
    assert spec.schedule == "0 8 * * *"
    assert spec.channel == "discord"
    assert spec.prompt == "Donne-moi la météo de Lyon pour aujourd'hui"
    assert spec.enabled is True
    assert spec.created_at == "2026-04-20T08:00:00Z"
    assert spec.description == "Météo matinale"
    assert spec.timezone == "Europe/Paris"


@pytest.mark.unit
def test_jobspec_is_frozen(tmp_path: Path) -> None:
    """JobSpec is a frozen dataclass and must not allow mutation.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_model import load_job_yaml

    path = write_yaml(tmp_path, "job.yaml", VALID_DATA)
    spec = load_job_yaml(path)

    with pytest.raises((AttributeError, TypeError)):
        spec.id = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Timezone default
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_timezone_defaults_to_utc(tmp_path: Path) -> None:
    """When ``timezone`` is absent from YAML the field defaults to ``"UTC"``.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_model import load_job_yaml

    data = {k: v for k, v in VALID_DATA.items() if k != "timezone"}
    path = write_yaml(tmp_path, "no-tz.yaml", data)
    spec = load_job_yaml(path)

    assert spec.timezone == "UTC"


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------


REQUIRED_FIELDS = ["id", "owner_id", "schedule", "channel", "prompt", "enabled", "created_at", "description"]


@pytest.mark.unit
@pytest.mark.parametrize("missing_field", REQUIRED_FIELDS)
def test_missing_required_field_raises_value_error(tmp_path: Path, missing_field: str) -> None:
    """Omitting any required field raises ValueError with a clear message.

    Args:
        tmp_path: Pytest-provided temporary directory.
        missing_field: Name of the field dropped from the YAML.
    """
    from horloger.job_model import load_job_yaml

    data = {k: v for k, v in VALID_DATA.items() if k != missing_field}
    path = write_yaml(tmp_path, f"missing-{missing_field}.yaml", data)

    with pytest.raises(ValueError) as exc_info:
        load_job_yaml(path)

    assert missing_field in str(exc_info.value), (
        f"ValueError message should mention the missing field '{missing_field}', "
        f"got: {exc_info.value}"
    )


# ---------------------------------------------------------------------------
# Invalid cron expression
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_invalid_cron_raises_value_error(tmp_path: Path) -> None:
    """An invalid cron expression raises ValueError.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_model import load_job_yaml

    data = {**VALID_DATA, "schedule": "not-a-cron"}
    path = write_yaml(tmp_path, "bad-cron.yaml", data)

    with pytest.raises(ValueError) as exc_info:
        load_job_yaml(path)

    assert "schedule" in str(exc_info.value).lower() or "cron" in str(exc_info.value).lower()


@pytest.mark.unit
def test_empty_cron_raises_value_error(tmp_path: Path) -> None:
    """An empty cron string raises ValueError.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_model import load_job_yaml

    data = {**VALID_DATA, "schedule": ""}
    path = write_yaml(tmp_path, "empty-cron.yaml", data)

    with pytest.raises(ValueError):
        load_job_yaml(path)


# ---------------------------------------------------------------------------
# Enabled field handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enabled_false_loads_correctly(tmp_path: Path) -> None:
    """``enabled: false`` is parsed as Python ``False``.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_model import load_job_yaml

    data = {**VALID_DATA, "enabled": False}
    path = write_yaml(tmp_path, "disabled.yaml", data)
    spec = load_job_yaml(path)

    assert spec.enabled is False


# ---------------------------------------------------------------------------
# Non-existent file
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_nonexistent_file_raises(tmp_path: Path) -> None:
    """Loading a path that does not exist raises an exception (FileNotFoundError or OSError).

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_model import load_job_yaml

    with pytest.raises((FileNotFoundError, OSError)):
        load_job_yaml(tmp_path / "does-not-exist.yaml")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_various_valid_cron_expressions(tmp_path: Path) -> None:
    """Several well-known valid cron patterns all load without error.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_model import load_job_yaml

    crons = [
        "* * * * *",
        "0 0 * * *",
        "*/15 * * * *",
        "0 9-17 * * 1-5",
        "0 0 1 * *",
    ]
    for cron in crons:
        data = {**VALID_DATA, "schedule": cron}
        path = write_yaml(tmp_path, f"cron-{cron.replace(' ', '_').replace('*', 'x').replace('/', 's')}.yaml", data)
        spec = load_job_yaml(path)
        assert spec.schedule == cron
