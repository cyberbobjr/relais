import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("archiviste.cleanup_retention")


@dataclass
class RetentionConfig:
    """Politique de rétention des fichiers archivés.

    Attributes:
        jsonl_days: Rétention des fichiers JSONL en jours.
        sqlite_days: Rétention des bases SQLite en jours.
        audit_days: Rétention des logs d'audit (``None`` = conservation infinie).
    """

    jsonl_days: int = 90
    sqlite_days: int = 365
    audit_days: int | None = None


class CleanupManager:
    """Gère la rétention des logs archivés dans un répertoire d'archive.

    Les fichiers JSONL plus anciens que ``RetentionConfig.jsonl_days`` sont
    supprimés lors de l'appel à ``cleanup_jsonl()``. La méthode ``run_daily()``
    orchestre l'ensemble des tâches de nettoyage à appeler une fois par jour.
    """

    def __init__(
        self,
        archive_dir: Path,
        config: RetentionConfig | None = None,
    ) -> None:
        """Initialise le gestionnaire de rétention.

        Args:
            archive_dir: Répertoire racine contenant les fichiers archivés.
            config: Politique de rétention. Utilise les valeurs par défaut si
                ``None``.
        """
        self._archive_dir = archive_dir
        self._config = config or RetentionConfig()

    async def cleanup_jsonl(self) -> int:
        """Supprime les fichiers JSONL plus vieux que ``jsonl_days``.

        Parcourt récursivement ``archive_dir`` et supprime tout fichier
        ``*.jsonl`` dont la date de dernière modification dépasse le seuil de
        rétention configuré.

        Returns:
            Nombre de fichiers supprimés.
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
        """Retourne les statistiques du répertoire d'archive.

        Collecte le nombre de fichiers JSONL, la taille totale et la date du
        fichier le plus ancien présent dans ``archive_dir``.

        Returns:
            Dict avec les clés ``file_count`` (int), ``total_bytes`` (int),
            ``oldest_mtime`` (float | None) et ``archive_dir`` (str).
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
        """Tâche de nettoyage complète à appeler une fois par jour.

        Exécute ``cleanup_jsonl()`` et journalise les statistiques avant et
        après le nettoyage.

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
