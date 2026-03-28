"""Prompt loader for Le Portail.

Loads named prompt templates from ~/.relais/prompts/ (user overrides) with
automatic fallback to the repo's prompts/ directory.  An in-memory cache
avoids repeated disk reads; entries are invalidated when the file's mtime
changes.
"""

import logging
import os
from pathlib import Path

from common.config_loader import get_relais_home

logger = logging.getLogger("portail.prompt_loader")

# Search paths, in resolution order.
_USER_PROMPTS_DIR = get_relais_home() / "prompts"
_REPO_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# Cache: name → (mtime, content)
_cache: dict[str, tuple[float, str]] = {}


def _resolve_path(name: str) -> Path | None:
    """Return the first existing prompt file for *name*, or None.

    Args:
        name: Prompt name without extension (e.g., "out_of_hours").

    Returns:
        Path to the .md file, or None if not found in either location.
    """
    for directory in (_USER_PROMPTS_DIR, _REPO_PROMPTS_DIR):
        candidate = directory / f"{name}.md"
        if candidate.exists():
            return candidate
    return None


def load_prompt(name: str) -> str:
    """Load a prompt template by name, using an mtime-aware cache.

    Resolution order:
      1. ~/.relais/prompts/{name}.md
      2. prompts/{name}.md  (repo-level fallback)

    The result is cached in memory.  If the file is modified on disk,
    the cache entry is invalidated automatically on the next call.

    Args:
        name: Prompt identifier without path or extension.

    Returns:
        The prompt text, or an empty string if the prompt is not found.
    """
    path = _resolve_path(name)
    if path is None:
        logger.debug("Prompt %r not found in any search path.", name)
        return ""

    try:
        current_mtime: float = os.path.getmtime(path)
    except OSError as exc:
        logger.warning("Cannot stat prompt file %s: %s", path, exc)
        return ""

    cached = _cache.get(name)
    if cached is not None:
        cached_mtime, cached_content = cached
        if cached_mtime == current_mtime:
            return cached_content

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to read prompt file %s: %s", path, exc)
        return ""

    _cache[name] = (current_mtime, content)
    logger.debug("Loaded prompt %r from %s", name, path)
    return content


def list_prompts() -> list[str]:
    """Return a deduplicated list of available prompt names.

    Scans both the user prompts directory and the repo prompts directory.
    Names from the user directory shadow repo-level names (listed once).

    Returns:
        Sorted list of prompt names (without .md extension).
    """
    seen: set[str] = set()
    names: list[str] = []

    for directory in (_USER_PROMPTS_DIR, _REPO_PROMPTS_DIR):
        if not directory.exists():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.suffix == ".md" and entry.is_file():
                prompt_name = entry.stem
                if prompt_name not in seen:
                    seen.add(prompt_name)
                    names.append(prompt_name)

    return sorted(names)
