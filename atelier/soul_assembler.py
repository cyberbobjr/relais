"""Soul assembler — builds the multi-layer system prompt for Atelier.

Layer assembly order:
  1. soul/SOUL.md               — core personality (always attempted)
  2. role_prompt_path           — role-level overlay (explicit path from portail.yaml)
  3. user_prompt_path           — per-user override (explicit path from portail.yaml)
  4. channel_prompt_path        — channel formatting rules (explicit path from aiguilleur.yaml)

All paths are explicit: nothing is inferred from role names, channel names, or
any other convention.  Missing or empty files are silently skipped (logged at
DEBUG level).  Layers are joined with "\\n\\n---\\n\\n".
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

_SEP = "\n\n---\n\n"


class AssemblyResult(NamedTuple):
    """Result returned by :func:`assemble_system_prompt`.

    Attributes:
        prompt: The assembled system prompt string (layers joined by ``_SEP``).
            Empty string when no layer contributes any content.
        issues: List of human-readable strings describing every layer file that
            could not be loaded (missing file, I/O error, or security rejection).
            Empty when all requested layers were loaded successfully.
        is_degraded: ``True`` when at least one layer file could not be loaded
            (i.e. ``issues`` is non-empty); ``False`` otherwise.
    """

    prompt: str
    issues: list[str]
    is_degraded: bool


def assemble_system_prompt(
    prompts_dir: str | Path,
    role_prompt_path: str | Path | None = None,
    user_prompt_path: str | Path | None = None,
    channel_prompt_path: str | Path | None = None,
) -> AssemblyResult:
    """Assemble a multi-layer system prompt from prompt fragments on disk.

    Reads up to four optional layers in a defined order and joins the non-empty
    ones with a horizontal-rule separator.  Missing or unreadable files are
    recorded in the returned :class:`AssemblyResult` so callers can detect and
    log prompt degradation.  The function never raises for missing files.

    All prompt paths (role, user, channel) must be relative to *prompts_dir*.
    Absolute paths and paths that escape *prompts_dir* are rejected with a
    WARNING, skipped, and recorded as issues.

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
            as configured in ``aiguilleur.yaml`` (``prompt_path`` field per channel),
            stamped into ``envelope.context["aiguilleur"]["channel_prompt_path"]`` by
            Aiguilleur.  ``None`` = no channel overlay loaded.

    Returns:
        An :class:`AssemblyResult` whose ``prompt`` field contains all present
        layers joined by ``"\\n\\n---\\n\\n"`` (empty string when no layer
        contributes content), ``issues`` lists every file that could not be
        loaded, and ``is_degraded`` is ``True`` when any issue was recorded.
    """
    base = Path(prompts_dir)
    layers: list[str] = []
    issues: list[str] = []

    # Layer 1 — soul personality
    _append_file(layers, base / "soul" / "SOUL.md", warn_if_missing=True, issues=issues)

    # Layer 2 — role overlay (explicit path, no convention inference)
    if role_prompt_path is not None:
        _append_explicit_path(layers, base, role_prompt_path, "role_prompt_path", issues=issues)

    # Layer 3 — per-user override (explicit path, no convention inference)
    if user_prompt_path is not None:
        _append_explicit_path(layers, base, user_prompt_path, "user_prompt_path", issues=issues)

    # Layer 4 — channel formatting (explicit path, no convention inference)
    if channel_prompt_path is not None:
        _append_explicit_path(layers, base, channel_prompt_path, "channel_prompt_path", issues=issues)

    return AssemblyResult(
        prompt=_SEP.join(layers),
        issues=issues,
        is_degraded=len(issues) > 0,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _append_explicit_path(
    layers: list[str],
    base: Path,
    path_arg: str | Path,
    label: str,
    *,
    issues: list[str],
) -> None:
    """Resolve an explicit prompt path and append its content to *layers*.

    Rejects absolute paths and paths that escape *base*.  Logs a WARNING for
    security violations; logs a WARNING when the file is missing (explicit
    paths should exist if configured).  Any rejection or missing-file event is
    recorded in *issues*.

    Args:
        layers: Accumulator list to append the file content to.
        base: Trusted root directory — resolved path must start with this.
        path_arg: The explicit path value from configuration (relative to base).
        label: Human-readable label used in log messages (e.g. ``"user_prompt_path"``).
        issues: Mutable list to record degradation reasons into.
    """
    p = Path(path_arg)
    if p.is_absolute():
        msg = f"{label} must be relative to prompts_dir, skipping: {p}"
        logger.warning("%s must be relative to prompts_dir, skipping: %s", label, p)
        issues.append(msg)
        return
    candidate = (base / p).resolve()
    if not str(candidate).startswith(str(base.resolve())):
        msg = f"{label} escapes prompts_dir, skipping: {candidate}"
        logger.warning("%s escapes prompts_dir, skipping: %s", label, candidate)
        issues.append(msg)
        return
    _append_file(layers, candidate, warn_if_missing=True, issues=issues)


def _append_file(
    layers: list[str],
    path: Path,
    *,
    warn_if_missing: bool = False,
    issues: list[str],
) -> None:
    """Read a prompt fragment file and append it to *layers* if non-empty.

    Silently skips empty files.  Missing files are logged (WARNING when
    *warn_if_missing* is True, otherwise DEBUG) and recorded in *issues*.
    I/O errors are also caught, logged, and recorded in *issues*.

    Args:
        layers: Accumulator list to append the file content to.
        path: Absolute path of the prompt fragment file to read.
        warn_if_missing: When True, emit a WARNING log if the file is absent
            instead of the usual DEBUG log.
        issues: Mutable list to record degradation reasons into.
    """
    if not path.exists():
        log_fn = logger.warning if warn_if_missing else logger.debug
        log_fn("Prompt file not found, skipping: %s", path)
        issues.append(f"Prompt file not found: {path}")
        return

    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("Prompt file unreadable, skipping: %s — %s", path, exc)
        issues.append(f"Prompt file unreadable: {path} — {exc}")
        return

    if not content:
        logger.debug("Prompt file is empty, skipping: %s", path)
        return

    logger.info("Loaded prompt layer: %s", path)
    layers.append(content)
