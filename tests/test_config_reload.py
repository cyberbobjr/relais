"""Tests for common.config_reload — safe_reload and checkpoint_good_config.

TDD — tests are written before the implementation.  All tests are unit tests
that mock filesystem and logging; no Redis or external dependency required.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lock() -> asyncio.Lock:
    """Return a fresh asyncio.Lock for testing.

    Returns:
        A new asyncio.Lock instance.
    """
    return asyncio.Lock()


# ---------------------------------------------------------------------------
# safe_reload — loader raises
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_reload_loader_raises_returns_false() -> None:
    """safe_reload returns False when loader() raises; self is untouched."""
    from common.config_reload import safe_reload

    lock = _make_lock()
    applied: list[object] = []

    def bad_loader():
        raise ValueError("broken YAML")

    def applier(candidate):
        applied.append(candidate)

    result = await safe_reload(lock, "test_brick", bad_loader, applier)

    assert result is False
    assert applied == [], "applier must NOT be called when loader fails"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_reload_loader_raises_logs_critical(caplog) -> None:
    """safe_reload logs at CRITICAL level when loader() raises."""
    from common.config_reload import safe_reload

    lock = _make_lock()

    def bad_loader():
        raise RuntimeError("parse error")

    with caplog.at_level(logging.CRITICAL, logger="common.config_reload"):
        await safe_reload(lock, "portail", bad_loader, lambda c: None)

    assert any("portail" in r.message or "portail" in r.name for r in caplog.records), (
        "Expected brick name in critical log message"
    )
    assert any(r.levelno == logging.CRITICAL for r in caplog.records), (
        "Expected CRITICAL level log record"
    )


# ---------------------------------------------------------------------------
# safe_reload — loader succeeds
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_reload_loader_succeeds_calls_applier() -> None:
    """safe_reload calls applier(candidate) when loader() succeeds."""
    from common.config_reload import safe_reload

    lock = _make_lock()
    applied: list[object] = []
    candidate_obj = {"key": "value"}

    def good_loader():
        return candidate_obj

    def applier(candidate):
        applied.append(candidate)

    result = await safe_reload(lock, "portail", good_loader, applier)

    assert result is True
    assert applied == [candidate_obj]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_reload_loader_succeeds_returns_true() -> None:
    """safe_reload returns True when loader() and applier() both succeed."""
    from common.config_reload import safe_reload

    lock = _make_lock()

    result = await safe_reload(lock, "sentinelle", lambda: "cfg", lambda c: None)

    assert result is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_reload_applier_called_under_lock() -> None:
    """applier is called while the lock is held (lock is acquired before call)."""
    from common.config_reload import safe_reload

    lock = _make_lock()
    lock_was_locked_during_apply: list[bool] = []

    def applier(candidate):
        # The lock should be locked when applier runs
        lock_was_locked_during_apply.append(lock.locked())

    await safe_reload(lock, "atelier", lambda: "cfg", applier)

    assert lock_was_locked_during_apply == [True], (
        "applier must be called while the lock is held"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_reload_lock_released_after_success() -> None:
    """The lock is released after a successful reload."""
    from common.config_reload import safe_reload

    lock = _make_lock()

    await safe_reload(lock, "portail", lambda: "cfg", lambda c: None)

    assert not lock.locked(), "lock must be released after successful reload"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_reload_lock_released_after_failure() -> None:
    """The lock is never acquired (no attempt) after loader failure, so it stays free."""
    from common.config_reload import safe_reload

    lock = _make_lock()

    await safe_reload(lock, "portail", lambda: (_ for _ in ()).throw(ValueError("fail")), lambda c: None)

    assert not lock.locked(), "lock must not be held after loader failure"


# ---------------------------------------------------------------------------
# safe_reload — applier raises (unexpected — should propagate cleanly)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_reload_applier_raises_propagates() -> None:
    """If applier raises, the exception propagates out of safe_reload."""
    from common.config_reload import safe_reload

    lock = _make_lock()

    def bad_applier(candidate):
        raise RuntimeError("unexpected write error")

    with pytest.raises(RuntimeError, match="unexpected write error"):
        await safe_reload(lock, "portail", lambda: "cfg", bad_applier)


# ---------------------------------------------------------------------------
# checkpoint_good_config
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_checkpoint_good_config_creates_backup(tmp_path: Path) -> None:
    """checkpoint_good_config writes a .bak copy under relais_home/config/backups/."""
    from common.config_reload import checkpoint_good_config

    # Create a fake YAML file to back up
    config_file = tmp_path / "portail.yaml"
    config_file.write_text("users: {}", encoding="utf-8")

    fake_home = tmp_path / "relais_home"

    with patch("common.config_reload.resolve_relais_home", return_value=fake_home):
        checkpoint_good_config(config_file)

    backup_path = fake_home / "config" / "backups" / "portail.yaml.bak"
    assert backup_path.exists(), f"Expected backup at {backup_path}"
    assert backup_path.read_text(encoding="utf-8") == "users: {}"


@pytest.mark.unit
def test_checkpoint_good_config_creates_parent_dirs(tmp_path: Path) -> None:
    """checkpoint_good_config creates intermediate directories if absent."""
    from common.config_reload import checkpoint_good_config

    config_file = tmp_path / "sentinelle.yaml"
    config_file.write_text("acl: {}", encoding="utf-8")

    fake_home = tmp_path / "new_relais_home"
    # fake_home does NOT exist yet

    with patch("common.config_reload.resolve_relais_home", return_value=fake_home):
        checkpoint_good_config(config_file)

    backup_path = fake_home / "config" / "backups" / "sentinelle.yaml.bak"
    assert backup_path.exists()


@pytest.mark.unit
def test_checkpoint_good_config_overwrites_existing_backup(tmp_path: Path) -> None:
    """checkpoint_good_config overwrites a pre-existing .bak file."""
    from common.config_reload import checkpoint_good_config

    config_file = tmp_path / "portail.yaml"
    config_file.write_text("new_content: true", encoding="utf-8")

    fake_home = tmp_path / "relais_home"
    backup_dir = fake_home / "config" / "backups"
    backup_dir.mkdir(parents=True)
    old_backup = backup_dir / "portail.yaml.bak"
    old_backup.write_text("old_content: true", encoding="utf-8")

    with patch("common.config_reload.resolve_relais_home", return_value=fake_home):
        checkpoint_good_config(config_file)

    assert old_backup.read_text(encoding="utf-8") == "new_content: true"


@pytest.mark.unit
def test_checkpoint_good_config_backup_name_uses_original_filename(tmp_path: Path) -> None:
    """Backup file name is <original>.bak regardless of path depth."""
    from common.config_reload import checkpoint_good_config

    config_file = tmp_path / "atelier.yaml"
    config_file.write_text("profiles: {}", encoding="utf-8")
    fake_home = tmp_path / "home"

    with patch("common.config_reload.resolve_relais_home", return_value=fake_home):
        checkpoint_good_config(config_file)

    backup_path = fake_home / "config" / "backups" / "atelier.yaml.bak"
    assert backup_path.exists()
    # No other .bak files created
    bak_files = list((fake_home / "config" / "backups").glob("*.bak"))
    assert len(bak_files) == 1


@pytest.mark.unit
def test_checkpoint_rotates_previous_backup(tmp_path: Path) -> None:
    """On second call, previous .bak becomes .bak.1 and new content goes to .bak."""
    from common.config_reload import checkpoint_good_config

    config_file = tmp_path / "portail.yaml"
    config_file.write_text("v1: true", encoding="utf-8")

    fake_home = tmp_path / "relais_home"

    with patch("common.config_reload.resolve_relais_home", return_value=fake_home):
        checkpoint_good_config(config_file)

    config_file.write_text("v2: true", encoding="utf-8")

    with patch("common.config_reload.resolve_relais_home", return_value=fake_home):
        checkpoint_good_config(config_file)

    backup_dir = fake_home / "config" / "backups"
    assert (backup_dir / "portail.yaml.bak").read_text(encoding="utf-8") == "v2: true"
    assert (backup_dir / "portail.yaml.bak.1").read_text(encoding="utf-8") == "v1: true"


@pytest.mark.unit
def test_checkpoint_keeps_max_5_backups(tmp_path: Path) -> None:
    """After 6 calls only 5 backup files are kept (.bak through .bak.4)."""
    from common.config_reload import checkpoint_good_config

    config_file = tmp_path / "portail.yaml"
    fake_home = tmp_path / "relais_home"

    for i in range(6):
        config_file.write_text(f"v{i}: true", encoding="utf-8")
        with patch("common.config_reload.resolve_relais_home", return_value=fake_home):
            checkpoint_good_config(config_file)

    backup_dir = fake_home / "config" / "backups"
    # Most recent = v5
    assert (backup_dir / "portail.yaml.bak").read_text(encoding="utf-8") == "v5: true"
    # Oldest kept = v1 (.bak.4), v0 was evicted
    assert (backup_dir / "portail.yaml.bak.4").read_text(encoding="utf-8") == "v1: true"
    assert not (backup_dir / "portail.yaml.bak.5").exists()
    # Exactly 5 files total
    all_bak = sorted(backup_dir.iterdir())
    assert len(all_bak) == 5


@pytest.mark.unit
def test_checkpoint_oldest_evicted_on_overflow(tmp_path: Path) -> None:
    """The oldest backup (.bak.4) is deleted when a 6th version arrives."""
    from common.config_reload import checkpoint_good_config

    config_file = tmp_path / "portail.yaml"
    fake_home = tmp_path / "relais_home"

    for i in range(5):
        config_file.write_text(f"v{i}: true", encoding="utf-8")
        with patch("common.config_reload.resolve_relais_home", return_value=fake_home):
            checkpoint_good_config(config_file)

    backup_dir = fake_home / "config" / "backups"
    # Before 6th: .bak.4 = v0
    assert (backup_dir / "portail.yaml.bak.4").read_text(encoding="utf-8") == "v0: true"

    config_file.write_text("v5: true", encoding="utf-8")
    with patch("common.config_reload.resolve_relais_home", return_value=fake_home):
        checkpoint_good_config(config_file)

    # v0 is gone
    assert not (backup_dir / "portail.yaml.bak.5").exists()
    assert (backup_dir / "portail.yaml.bak.4").read_text(encoding="utf-8") == "v1: true"
