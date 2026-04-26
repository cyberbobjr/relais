"""Soul assembler — resolves and validates multi-layer prompt file paths for Atelier.

Layer resolution order:
  1. soul/SOUL.md               — core personality (always attempted)
  2. role_prompt_path           — role-level overlay (explicit path from portail.yaml)
  3. user_prompt_path           — per-user override (explicit path from portail.yaml)
  4. channel_prompt_path        — channel formatting rules (explicit path from aiguilleur.yaml)

Returns a list of validated absolute path strings ready for DeepAgents ``memory=`` parameter.
File reading is delegated to DeepAgents — this module only validates paths.

All paths are explicit: nothing is inferred from role names, channel names, or
any other convention.  Missing or invalid files are excluded from the result and
recorded in ``AssemblyResult.issues``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)


class AssemblyResult(NamedTuple):
    """Result returned by :func:`assemble_system_prompt`.

    Attributes:
        memory_paths: Ordered list of validated absolute path strings ready to
            pass as ``memory=`` to ``create_deep_agent()``.  Only paths that
            exist and stay within *prompts_dir* are included.
        issues: List of human-readable strings describing every layer file that
            could not be validated (missing file, security rejection).
            Empty when all requested layers were validated successfully.
        is_degraded: ``True`` when at least one layer file could not be validated
            (i.e. ``issues`` is non-empty); ``False`` otherwise.
    """

    memory_paths: list[str]
    issues: list[str]
    is_degraded: bool


def assemble_system_prompt(
    prompts_dir: str | Path,
    role_prompt_path: str | Path | None = None,
    user_prompt_path: str | Path | None = None,
    channel_prompt_path: str | Path | None = None,
) -> AssemblyResult:
    """Validate and resolve multi-layer prompt file paths.

    Checks up to four optional layers in a defined order and returns the
    absolute path of each file that passes validation (exists + within jail).
    Missing or invalid files are recorded in the returned :class:`AssemblyResult`
    so callers can detect and log prompt degradation.  Never raises for missing files.

    All prompt paths (role, user, channel) must be relative to *prompts_dir*.
    Absolute paths and paths that escape *prompts_dir* are rejected with a
    WARNING, excluded, and recorded as issues.

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
        An :class:`AssemblyResult` whose ``memory_paths`` field contains ordered
        absolute path strings for all valid layers, ``issues`` lists every file
        that could not be validated, and ``is_degraded`` is ``True`` when any
        issue was recorded.
    """
    base = Path(prompts_dir)
    paths: list[str] = []
    issues: list[str] = []

    # Layer 1 — soul personality
    _resolve_fixed_path(paths, base / "soul" / "SOUL.md", issues=issues)

    # Layer 2 — role overlay (explicit path, no convention inference)
    if role_prompt_path is not None:
        _resolve_explicit_path(paths, base, role_prompt_path, "role_prompt_path", issues=issues)

    # Layer 3 — per-user override (explicit path, no convention inference)
    if user_prompt_path is not None:
        _resolve_explicit_path(paths, base, user_prompt_path, "user_prompt_path", issues=issues)

    # Layer 4 — channel formatting (explicit path, no convention inference)
    if channel_prompt_path is not None:
        _resolve_explicit_path(paths, base, channel_prompt_path, "channel_prompt_path", issues=issues)

    return AssemblyResult(
        memory_paths=paths,
        issues=issues,
        is_degraded=len(issues) > 0,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_explicit_path(
    paths: list[str],
    base: Path,
    path_arg: str | Path,
    label: str,
    *,
    issues: list[str],
) -> None:
    """Resolve an explicit prompt path and append it to *paths* if valid.

    Rejects absolute paths and paths that escape *base*.  Logs a WARNING for
    security violations; logs a WARNING when the file is missing.  Any
    rejection or missing-file event is recorded in *issues*.

    Args:
        paths: Accumulator list to append the validated absolute path string to.
        base: Trusted root directory — resolved path must start with this.
        path_arg: The explicit path value from configuration (relative to base).
        label: Human-readable label used in log messages (e.g. ``"user_prompt_path"``).
        issues: Mutable list to record degradation reasons into.
    """
    p = Path(path_arg)
    if p.is_absolute():
        msg = f"{label} must be relative to prompts_dir, got absolute path: {p}"
        logger.warning(msg)
        issues.append(msg)
        return
    candidate = (base / p).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        msg = f"{label} escapes prompts_dir, skipping: {candidate}"
        logger.warning(msg)
        issues.append(msg)
        return
    _resolve_fixed_path(paths, candidate, issues=issues)


def _resolve_fixed_path(
    paths: list[str],
    path: Path,
    *,
    issues: list[str],
) -> None:
    """Check that a fixed path exists and append its absolute string to *paths*.

    Missing files are logged at WARNING and recorded in *issues*.

    Args:
        paths: Accumulator list to append the validated absolute path string to.
        path: Absolute path of the prompt file to validate.
        issues: Mutable list to record degradation reasons into.
    """
    if not path.exists():
        logger.warning("Prompt file not found, skipping: %s", path)
        issues.append(f"Prompt file not found: {path}")
        return
    if path.stat().st_size == 0:
        logger.warning("Prompt file is empty, skipping: %s", path)
        issues.append(f"Prompt file is empty: {path}")
        return
    logger.info("Resolved prompt layer: %s", path)
    paths.append(str(path))
