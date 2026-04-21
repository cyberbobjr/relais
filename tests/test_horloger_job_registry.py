"""Tests for horloger.job_registry — written FIRST per TDD (RED phase).

Tests cover:
- load_all with empty directory returns empty dict
- load_all scans multiple YAML files and returns all valid jobs
- load_all skips invalid/corrupt files gracefully and logs a warning
- reload picks up newly added files
- get returns None for an unknown job_id
- get returns the correct JobSpec for a known job_id
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_job_yaml(jobs_dir: Path, job_id: str, overrides: dict | None = None) -> Path:
    """Write a valid job YAML file into *jobs_dir* and return the path.

    Args:
        jobs_dir: Directory where the YAML is written.
        job_id: Job identifier; used as the file stem and the ``id`` field.
        overrides: Optional dict merged into the default valid job data.

    Returns:
        Path to the created YAML file.
    """
    data: dict = {
        "id": job_id,
        "owner_id": "usr_alice",
        "schedule": "0 8 * * *",
        "channel": "discord",
        "prompt": f"prompt for {job_id}",
        "enabled": True,
        "created_at": "2026-04-20T08:00:00Z",
        "description": f"Description for {job_id}",
        "timezone": "Europe/Paris",
    }
    if overrides:
        data.update(overrides)
    path = jobs_dir / f"{job_id}.yaml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# load_all — empty directory
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_all_empty_dir(tmp_path: Path) -> None:
    """load_all on an empty directory returns an empty dict.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_registry import JobRegistry

    registry = JobRegistry(tmp_path)
    result = registry.load_all()

    assert result == {}


# ---------------------------------------------------------------------------
# load_all — multiple valid files
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_all_multiple_files(tmp_path: Path) -> None:
    """load_all scans all *.yaml files and returns one entry per valid job.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_registry import JobRegistry

    write_job_yaml(tmp_path, "job-alpha")
    write_job_yaml(tmp_path, "job-beta")
    write_job_yaml(tmp_path, "job-gamma")

    registry = JobRegistry(tmp_path)
    result = registry.load_all()

    assert set(result.keys()) == {"job-alpha", "job-beta", "job-gamma"}
    assert result["job-alpha"].owner_id == "usr_alice"


# ---------------------------------------------------------------------------
# load_all — skips invalid files
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_all_skips_invalid_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """load_all skips files that fail validation and logs a warning.

    Args:
        tmp_path: Pytest-provided temporary directory.
        caplog: Pytest log capture fixture.
    """
    from horloger.job_registry import JobRegistry

    write_job_yaml(tmp_path, "valid-job")
    # Missing required field "id"
    broken_path = tmp_path / "broken.yaml"
    broken_path.write_text(yaml.dump({"owner_id": "usr_bob", "schedule": "* * * * *"}), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="horloger.job_registry"):
        registry = JobRegistry(tmp_path)
        result = registry.load_all()

    assert "valid-job" in result
    assert "broken" not in result
    # A warning must have been emitted for the broken file
    assert any("broken" in record.message or "broken.yaml" in record.message for record in caplog.records)


@pytest.mark.unit
def test_load_all_skips_unparseable_yaml(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """load_all skips files containing invalid YAML syntax and logs a warning.

    Args:
        tmp_path: Pytest-provided temporary directory.
        caplog: Pytest log capture fixture.
    """
    from horloger.job_registry import JobRegistry

    write_job_yaml(tmp_path, "good-job")
    garbage_path = tmp_path / "garbage.yaml"
    garbage_path.write_text(": : : invalid yaml {{{", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="horloger.job_registry"):
        registry = JobRegistry(tmp_path)
        result = registry.load_all()

    assert "good-job" in result
    assert "garbage" not in result
    assert any("garbage" in record.message or "garbage.yaml" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# get — unknown and known IDs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_returns_none_for_unknown_id(tmp_path: Path) -> None:
    """get returns None when the requested job_id is not in the registry.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_registry import JobRegistry

    write_job_yaml(tmp_path, "known-job")
    registry = JobRegistry(tmp_path)
    registry.load_all()

    result = registry.get("does-not-exist")

    assert result is None


@pytest.mark.unit
def test_get_returns_correct_spec(tmp_path: Path) -> None:
    """get returns the correct JobSpec for a job that was loaded.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_registry import JobRegistry

    write_job_yaml(tmp_path, "my-job")
    registry = JobRegistry(tmp_path)
    registry.load_all()

    spec = registry.get("my-job")

    assert spec is not None
    assert spec.id == "my-job"
    assert spec.channel == "discord"


@pytest.mark.unit
def test_get_returns_none_before_load(tmp_path: Path) -> None:
    """get returns None when load_all has not been called yet.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_registry import JobRegistry

    write_job_yaml(tmp_path, "my-job")
    registry = JobRegistry(tmp_path)

    # No load_all() called — internal dict is empty
    result = registry.get("my-job")

    assert result is None


# ---------------------------------------------------------------------------
# reload — picks up new files
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reload_picks_up_new_files(tmp_path: Path) -> None:
    """reload re-scans the directory and returns previously absent jobs.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_registry import JobRegistry

    write_job_yaml(tmp_path, "first-job")
    registry = JobRegistry(tmp_path)
    initial = registry.load_all()
    assert set(initial.keys()) == {"first-job"}

    # Add a second job after initial load
    write_job_yaml(tmp_path, "second-job")
    refreshed = registry.reload()

    assert set(refreshed.keys()) == {"first-job", "second-job"}
    # Internal state updated too
    assert registry.get("second-job") is not None


@pytest.mark.unit
def test_reload_drops_deleted_files(tmp_path: Path) -> None:
    """reload reflects file deletions — removed jobs are no longer returned.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_registry import JobRegistry

    write_job_yaml(tmp_path, "keep-job")
    transient_path = write_job_yaml(tmp_path, "transient-job")
    registry = JobRegistry(tmp_path)
    registry.load_all()
    assert registry.get("transient-job") is not None

    # Delete the transient file
    transient_path.unlink()
    registry.reload()

    assert registry.get("transient-job") is None
    assert registry.get("keep-job") is not None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_all_ignores_non_yaml_files(tmp_path: Path) -> None:
    """load_all ignores files that do not end in ``.yaml``.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_registry import JobRegistry

    (tmp_path / "readme.txt").write_text("not a job", encoding="utf-8")
    (tmp_path / "job.json").write_text('{"id": "json-job"}', encoding="utf-8")
    write_job_yaml(tmp_path, "real-job")

    registry = JobRegistry(tmp_path)
    result = registry.load_all()

    assert set(result.keys()) == {"real-job"}


@pytest.mark.unit
def test_load_all_returns_dict_keyed_by_job_id(tmp_path: Path) -> None:
    """The dict returned by load_all is keyed by the ``id`` field, not the file name.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from horloger.job_registry import JobRegistry

    # File stem is "file-name" but job id is "actual-id"
    data: dict = {
        "id": "actual-id",
        "owner_id": "usr_alice",
        "schedule": "0 8 * * *",
        "channel": "discord",
        "prompt": "test prompt",
        "enabled": True,
        "created_at": "2026-04-20T08:00:00Z",
        "description": "test",
        "timezone": "UTC",
    }
    (tmp_path / "file-name.yaml").write_text(yaml.dump(data), encoding="utf-8")

    registry = JobRegistry(tmp_path)
    result = registry.load_all()

    assert "actual-id" in result
    assert "file-name" not in result
