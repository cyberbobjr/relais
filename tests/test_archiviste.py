"""Unit tests for archiviste.cleanup_retention and aiguilleur.base."""

import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# CleanupManager tests
# ---------------------------------------------------------------------------

from archiviste.cleanup_retention import CleanupManager, RetentionConfig


@pytest.fixture
def archive_dir(tmp_path: Path) -> Path:
    """Return a temporary archive directory."""
    d = tmp_path / "archive"
    d.mkdir()
    return d


@pytest.fixture
def manager(archive_dir: Path) -> CleanupManager:
    """Return a CleanupManager with a 30-day JSONL retention."""
    return CleanupManager(archive_dir=archive_dir, config=RetentionConfig(jsonl_days=30))


def _create_jsonl(directory: Path, name: str, age_days: float) -> Path:
    """Create a .jsonl file whose mtime is *age_days* days in the past."""
    path = directory / name
    path.write_text('{"event": "test"}\n', encoding="utf-8")
    old_mtime = time.time() - age_days * 86400
    import os
    os.utime(path, (old_mtime, old_mtime))
    return path


@pytest.mark.asyncio
async def test_cleanup_jsonl_deletes_old_files(manager: CleanupManager, archive_dir: Path) -> None:
    """cleanup_jsonl() doit supprimer les fichiers JSONL plus vieux que jsonl_days."""
    old_file = _create_jsonl(archive_dir, "old.jsonl", age_days=60)

    deleted = await manager.cleanup_jsonl()

    assert deleted == 1
    assert not old_file.exists()


@pytest.mark.asyncio
async def test_cleanup_jsonl_keeps_recent_files(manager: CleanupManager, archive_dir: Path) -> None:
    """cleanup_jsonl() doit conserver les fichiers récents."""
    recent_file = _create_jsonl(archive_dir, "recent.jsonl", age_days=5)

    deleted = await manager.cleanup_jsonl()

    assert deleted == 0
    assert recent_file.exists()


@pytest.mark.asyncio
async def test_cleanup_jsonl_returns_correct_count(
    manager: CleanupManager, archive_dir: Path
) -> None:
    """cleanup_jsonl() doit retourner le nombre exact de fichiers supprimés."""
    _create_jsonl(archive_dir, "old1.jsonl", age_days=90)
    _create_jsonl(archive_dir, "old2.jsonl", age_days=45)
    _create_jsonl(archive_dir, "recent.jsonl", age_days=10)

    deleted = await manager.cleanup_jsonl()

    assert deleted == 2


@pytest.mark.asyncio
async def test_get_stats_returns_correct_nb_files_and_total_size(
    manager: CleanupManager, archive_dir: Path
) -> None:
    """get_stats() doit retourner le bon nb_files (file_count) et total_bytes."""
    content = '{"event": "a"}\n'
    f1 = archive_dir / "a.jsonl"
    f2 = archive_dir / "b.jsonl"
    f1.write_text(content, encoding="utf-8")
    f2.write_text(content, encoding="utf-8")

    stats = await manager.get_stats()

    assert stats["file_count"] == 2
    expected_bytes = f1.stat().st_size + f2.stat().st_size
    assert stats["total_bytes"] == expected_bytes


@pytest.mark.asyncio
async def test_get_stats_empty_directory(manager: CleanupManager) -> None:
    """get_stats() doit retourner file_count=0 et total_bytes=0 si le dossier est vide."""
    stats = await manager.get_stats()

    assert stats["file_count"] == 0
    assert stats["total_bytes"] == 0
    assert stats["oldest_mtime"] is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_run_daily_deletes_old_files_and_returns_none(
    manager: CleanupManager, archive_dir: Path
) -> None:
    """run_daily() doit supprimer les fichiers anciens et retourner None."""
    old_file = _create_jsonl(archive_dir, "stale.jsonl", age_days=60)
    recent_file = _create_jsonl(archive_dir, "fresh.jsonl", age_days=5)

    result = await manager.run_daily()

    assert result is None
    assert not old_file.exists()
    assert recent_file.exists()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_run_daily_on_empty_directory_does_not_raise(
    manager: CleanupManager,
) -> None:
    """run_daily() sur un répertoire vide ne doit pas lever d'exception."""
    result = await manager.run_daily()
    assert result is None


# ---------------------------------------------------------------------------
# AiguilleurBase tests
# ---------------------------------------------------------------------------

import pytest_asyncio
from aiguilleur.base import AiguilleurBase
from common.envelope import Envelope


def test_aiguilleur_base_is_abstract() -> None:
    """AiguilleurBase est une ABC : l'instanciation directe doit lever TypeError."""
    with pytest.raises(TypeError):
        AiguilleurBase()  # type: ignore[abstract]


def test_concrete_aiguilleur_can_be_instantiated() -> None:
    """Une implémentation concrète qui implémente toutes les méthodes abstraites peut être instanciée."""

    class ConcreteAiguilleur(AiguilleurBase):
        channel_name = "test"

        async def receive(self) -> Envelope:
            return Envelope(
                content="",
                sender_id="u",
                channel="test",
                session_id="s",
            )

        async def send(self, envelope: Envelope, text: str) -> None:
            pass

        def format_for_channel(self, text: str) -> str:
            return text

    adapter = ConcreteAiguilleur()
    assert adapter.channel_name == "test"


@pytest.mark.asyncio
async def test_start_is_noop_by_default() -> None:
    """start() est un no-op par défaut et ne lève aucune exception."""

    class ConcreteAiguilleur(AiguilleurBase):
        channel_name = "noop"

        async def receive(self) -> Envelope:
            return Envelope(content="", sender_id="u", channel="noop", session_id="s")

        async def send(self, envelope: Envelope, text: str) -> None:
            pass

        def format_for_channel(self, text: str) -> str:
            return text

    adapter = ConcreteAiguilleur()
    # Should complete without raising
    await adapter.start()


@pytest.mark.asyncio
async def test_stop_is_noop_by_default() -> None:
    """stop() est un no-op par défaut et ne lève aucune exception."""

    class ConcreteAiguilleur(AiguilleurBase):
        channel_name = "noop"

        async def receive(self) -> Envelope:
            return Envelope(content="", sender_id="u", channel="noop", session_id="s")

        async def send(self, envelope: Envelope, text: str) -> None:
            pass

        def format_for_channel(self, text: str) -> str:
            return text

    adapter = ConcreteAiguilleur()
    # Should complete without raising
    await adapter.stop()


def test_channel_name_default_empty() -> None:
    """AiguilleurBase.channel_name defaults to empty string on the base class."""
    assert AiguilleurBase.channel_name == ""


def test_concrete_subclass_requires_all_abstractmethods() -> None:
    """A subclass that omits any abstract method raises TypeError on instantiation."""

    class IncompleteAiguilleur(AiguilleurBase):
        # Missing: receive, send, format_for_channel
        channel_name = "incomplete"

    with pytest.raises(TypeError):
        IncompleteAiguilleur()  # type: ignore[abstract]

    class MissingFormat(AiguilleurBase):
        channel_name = "partial"

        async def receive(self) -> Envelope:
            return Envelope(content="", sender_id="u", channel="partial", session_id="s")

        async def send(self, envelope: Envelope, text: str) -> None:
            pass
        # format_for_channel not implemented

    with pytest.raises(TypeError):
        MissingFormat()  # type: ignore[abstract]
