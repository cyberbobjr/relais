import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("archiviste.cleanup_retention")


@dataclass
class RetentionConfig:
    """Retention policy for archived files.

    Attributes:
        jsonl_days: Retention period for JSONL files in days.
        sqlite_days: Retention period for SQLite databases in days.
        audit_days: Retention period for audit logs (``None`` = keep forever).
    """

    jsonl_days: int = 90
    sqlite_days: int = 365
    audit_days: int | None = None


class CleanupManager:
    """Manage retention of archived logs in an archive directory.

    JSONL files older than ``RetentionConfig.jsonl_days`` are deleted when
    ``cleanup_jsonl()`` is called. The ``run_daily()`` method orchestrates all
    cleanup tasks and should be called once per day.
    """

    def __init__(
        self,
        archive_dir: Path,
        config: RetentionConfig | None = None,
    ) -> None:
        """Initialise the retention manager.

        Args:
            archive_dir: Root directory containing the archived files.
            config: Retention policy. Uses default values if ``None``.
        """
        self._archive_dir = archive_dir
        self._config = config or RetentionConfig()

    async def cleanup_jsonl(self) -> int:
        """Delete JSONL files older than ``jsonl_days``.

        Recursively walks ``archive_dir`` and deletes any ``*.jsonl`` file
        whose last modification time exceeds the configured retention threshold.

        Returns:
            Number of files deleted.
        """
        threshold = time.time() - self._config.jsonl_days * 86400
        deleted = 0
        for path in self._archive_dir.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime < threshold:
                    path.unlink()
                    deleted += 1
                    logger.info("Deleted stale JSONL: %s", path)
            except OSError as exc:
                logger.warning("Could not delete %s: %s", path, exc)
        return deleted

    async def get_stats(self) -> dict:
        """Return statistics for the archive directory.

        Collects the number of JSONL files, total size, and modification time of
        the oldest file present in ``archive_dir``.

        Returns:
            Dict with keys ``file_count`` (int), ``total_bytes`` (int),
            ``oldest_mtime`` (float | None) and ``archive_dir`` (str).
        """
        jsonl_files = list(self._archive_dir.rglob("*.jsonl"))
        if not jsonl_files:
            return {
                "file_count": 0,
                "total_bytes": 0,
                "oldest_mtime": None,
                "archive_dir": str(self._archive_dir),
            }

        total_bytes = 0
        oldest_mtime: float | None = None

        for path in jsonl_files:
            try:
                stat = path.stat()
                total_bytes += stat.st_size
                if oldest_mtime is None or stat.st_mtime < oldest_mtime:
                    oldest_mtime = stat.st_mtime
            except OSError as exc:
                logger.warning("Could not stat %s: %s", path, exc)

        return {
            "file_count": len(jsonl_files),
            "total_bytes": total_bytes,
            "oldest_mtime": oldest_mtime,
            "archive_dir": str(self._archive_dir),
        }

    async def run_daily(self) -> None:
        """Complete cleanup task to be called once per day.

        Runs ``cleanup_jsonl()`` and logs statistics before and after cleanup.

        Returns:
            None
        """
        logger.info("Starting daily cleanup in %s", self._archive_dir)
        stats_before = await self.get_stats()
        logger.info(
            "Archive stats before cleanup: %d files, %d bytes",
            stats_before["file_count"],
            stats_before["total_bytes"],
        )

        deleted = await self.cleanup_jsonl()
        logger.info("Deleted %d stale JSONL file(s)", deleted)

        stats_after = await self.get_stats()
        logger.info(
            "Archive stats after cleanup: %d files, %d bytes",
            stats_after["file_count"],
            stats_after["total_bytes"],
        )
