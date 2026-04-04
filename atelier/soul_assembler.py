"""Soul assembler — builds the multi-layer system prompt for Atelier.

Layer assembly order:
  1. soul/SOUL.md               — core personality (always attempted)
  2. role_prompt_path           — role-level overlay (explicit path from portail.yaml)
  3. user_prompt_path           — per-user override (explicit path from portail.yaml)
  4. channel_prompt_path        — channel formatting rules (explicit path from channels.yaml)

All paths are explicit: nothing is inferred from role names, channel names, or
any other convention.  Missing or empty files are silently skipped (logged at
DEBUG level).  Layers are joined with "\\n\\n---\\n\\n".
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SEP = "\n\n---\n\n"


def assemble_system_prompt(
    prompts_dir: str | Path,
    role_prompt_path: str | Path | None = None,
    user_prompt_path: str | Path | None = None,
    channel_prompt_path: str | Path | None = None,
) -> str:
    """Assemble a multi-layer system prompt from prompt fragments on disk.

    Reads up to four optional layers in a defined order and joins the non-empty
    ones with a horizontal-rule separator.  Missing or empty files are silently
    skipped.  The function never raises for missing files.

    All prompt paths (role, user, channel) must be relative to *prompts_dir*.
    Absolute paths and paths that escape *prompts_dir* are rejected with a
    WARNING and skipped.

    Args:
        prompts_dir: Root directory that contains the soul/, roles/,
            channels/, and policies/ sub-directories.
        role_prompt_path: Relative path to the role-level prompt overlay, as
            configured in ``portail.yaml`` (``roles[*].prompt_path`` field),
            relative to *prompts_dir*.  ``None`` = no role overlay loaded.
        user_prompt_path: Relative path to a per-user prompt override file,
            as configured in ``portail.yaml`` (``users[*].prompt_path`` field),
            relative to *prompts_dir*.  ``None`` = no user overlay loaded.
        channel_prompt_path: Relative path to the channel formatting overlay,
            as configured in ``channels.yaml`` (``prompt_path`` field per channel),
            stamped into ``envelope.metadata["channel_prompt_path"]`` by
            Aiguilleur.  ``None`` = no channel overlay loaded.

    Returns:
        A single string containing all present layers joined by
        ``"\\n\\n---\\n\\n"``.  Returns ``""`` when no layer contributes any
        content.
    """
    base = Path(prompts_dir)
    layers: list[str] = []

    # Layer 1 — soul personality
    _append_file(layers, base / "soul" / "SOUL.md", warn_if_missing=True)

    # Layer 2 — role overlay (explicit path, no convention inference)
    if role_prompt_path is not None:
        _append_explicit_path(layers, base, role_prompt_path, "role_prompt_path")

    # Layer 3 — per-user override (explicit path, no convention inference)
    if user_prompt_path is not None:
        _append_explicit_path(layers, base, user_prompt_path, "user_prompt_path")

    # Layer 4 — channel formatting (explicit path, no convention inference)
    if channel_prompt_path is not None:
        _append_explicit_path(layers, base, channel_prompt_path, "channel_prompt_path")

    return _SEP.join(layers)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _append_explicit_path(
    layers: list[str],
    base: Path,
    path_arg: str | Path,
    label: str,
) -> None:
    """Resolve an explicit prompt path and append its content to *layers*.

    Rejects absolute paths and paths that escape *base*.  Logs a WARNING for
    security violations; logs a WARNING when the file is missing (explicit
    paths should exist if configured).

    Args:
        layers: Accumulator list to append the file content to.
        base: Trusted root directory — resolved path must start with this.
        path_arg: The explicit path value from configuration (relative to base).
        label: Human-readable label used in log messages (e.g. ``"user_prompt_path"``).
    """
    p = Path(path_arg)
    if p.is_absolute():
        logger.warning("%s must be relative to prompts_dir, skipping: %s", label, p)
        return
    candidate = (base / p).resolve()
    if not str(candidate).startswith(str(base.resolve())):
        logger.warning("%s escapes prompts_dir, skipping: %s", label, candidate)
        return
    _append_file(layers, candidate, warn_if_missing=True)


def _append_file(layers: list[str], path: Path, *, warn_if_missing: bool = False) -> None:
    """Read a prompt fragment file and append it to *layers* if non-empty.

    Silently skips missing or empty files.  Logs at DEBUG (or WARNING when
    *warn_if_missing* is True and the file does not exist).

    Args:
        layers: Accumulator list to append the file content to.
        path: Absolute path of the prompt fragment file to read.
        warn_if_missing: When True, emit a WARNING log if the file is absent
            instead of the usual DEBUG log.
    """
    if not path.exists():
        log_fn = logger.warning if warn_if_missing else logger.debug
        log_fn("Prompt file not found, skipping: %s", path)
        return

    content = path.read_text(encoding="utf-8").strip()
    if not content:
        logger.debug("Prompt file is empty, skipping: %s", path)
        return

    logger.info("Loaded prompt layer: %s", path)
    layers.append(content)
