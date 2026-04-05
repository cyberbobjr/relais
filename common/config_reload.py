"""Hot-reload helper for RELAIS bricks.

Provides three reusable primitives:

* ``safe_reload(lock, label, loader, applier)`` — atomically swaps configuration
  using a caller-supplied asyncio lock.  If the loader raises, the current
  configuration is preserved and False is returned.

* ``checkpoint_good_config(path)`` — copies the given config file to a .bak
  backup in ``<relais_home>/config/backups/``.  Called after a successful
  reload so operators always have a known-good snapshot.

* ``watch_and_reload(paths, reload_fn, label)`` — watches config files for
  changes using watchfiles and calls reload_fn on any change.  Runs indefinitely.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from common.config_loader import get_relais_home as resolve_relais_home

logger = logging.getLogger("common.config_reload")

# watchfiles is an optional dependency used only by watch_and_reload().
# Imported at module level so tests can patch ``common.config_reload.watchfiles``.
# Set to None when the package is not installed; watch_and_reload() will raise
# a descriptive ImportError in that case.
try:
    import watchfiles
except ImportError:
    watchfiles = None  # type: ignore[assignment]

T = TypeVar("T")


async def safe_reload(
    lock: asyncio.Lock,
    label: str,
    loader: Callable[[], T],
    applier: Callable[[T], None],
    checkpoint_paths: list[Path] | None = None,
) -> bool:
    """Atomically reload configuration, preserving the current state on failure.

    Calls ``loader()`` in isolation first.  If it raises, logs at CRITICAL and
    returns False without touching any shared state.  If it succeeds, acquires
    ``lock`` and calls ``applier(candidate)`` to swap in the new configuration,
    then checkpoints each path in ``checkpoint_paths``.

    Args:
        lock: asyncio.Lock protecting the shared configuration object.
        label: Human-readable brick or component name used in log messages.
        loader: Zero-argument callable that constructs a new configuration
            object from disk.  May raise any exception on failure.
        applier: Single-argument callable that swaps the new configuration into
            the brick's state.  Called while ``lock`` is held.
        checkpoint_paths: Optional list of config file paths to back up after a
            successful reload.  Each path is copied to
            ``<relais_home>/config/backups/<filename>.bak``.

    Returns:
        True when the configuration was successfully reloaded and applied.
        False when ``loader()`` raised (configuration unchanged).

    Raises:
        Any exception raised by ``applier`` propagates to the caller.
    """
    try:
        candidate = loader()
    except Exception as exc:  # noqa: BLE001
        logger.critical(
            "[%s] Config reload failed — keeping previous configuration. Error: %s",
            label,
            exc,
        )
        return False

    async with lock:
        applier(candidate)

    for path in checkpoint_paths or []:
        try:
            checkpoint_good_config(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] Failed to checkpoint %s: %s", label, path, exc)

    return True


_MAX_BACKUPS = 5


def checkpoint_good_config(path: Path) -> None:
    """Save a known-good config file as a .bak backup, keeping the last 5 versions.

    Backup naming (newest first):
      ``<filename>.bak``       — most recent
      ``<filename>.bak.1``     — previous
      …
      ``<filename>.bak.4``     — oldest retained

    On each call the existing backups are rotated: ``.bak.4`` is evicted,
    each ``.bak.N`` becomes ``.bak.N+1``, and ``.bak`` becomes ``.bak.1``.
    The new snapshot is then written as ``.bak``.

    Parent directories are created automatically.  Call this function only
    after a successful ``safe_reload``.

    Args:
        path: Absolute path to the config file that was just successfully
            reloaded.  Only the filename (not the full path) is used for the
            backup name.
    """
    backup_dir = resolve_relais_home() / "config" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    base = backup_dir / (path.name + ".bak")

    # Rotate: evict oldest, shift remaining up by 1
    oldest = Path(f"{base}.{_MAX_BACKUPS - 1}")
    if oldest.exists():
        oldest.unlink()
    for i in range(_MAX_BACKUPS - 2, 0, -1):
        src = Path(f"{base}.{i}")
        if src.exists():
            src.rename(Path(f"{base}.{i + 1}"))
    if base.exists():
        base.rename(Path(f"{base}.1"))

    shutil.copy2(str(path), str(base))
    logger.debug("Checkpointed good config: %s → %s", path, base)


async def watch_and_reload(
    paths: list[Path],
    reload_fn: Callable[[], Awaitable[bool]],
    label: str,
) -> None:
    """Watch config files for changes and trigger hot-reloads on any modification.

    Uses ``watchfiles.awatch()`` to monitor the given paths.  On any change
    event, calls ``reload_fn()`` and logs the outcome.  Runs indefinitely until
    the calling task is cancelled.

    The ``watchfiles`` package is imported lazily so that this module remains
    importable even when watchfiles is not installed.  An ImportError with a
    helpful installation message is raised at call time if watchfiles is absent.

    Args:
        paths: List of filesystem paths to watch for changes.
        reload_fn: Async callable that performs the reload and returns True on
            success or False on failure.
        label: Human-readable brick or component name used in log messages.

    Raises:
        ImportError: When the ``watchfiles`` package is not installed.
    """
    if watchfiles is None:
        raise ImportError(
            "watchfiles is required for hot-reload: pip install watchfiles"
        )

    async for _changes in watchfiles.awatch(*paths):
        logger.info("[%s] config file changed, reloading…", label)
        success = await reload_fn()
        if not success:
            logger.critical(
                "[%s] reload failed — keeping previous config", label
            )
