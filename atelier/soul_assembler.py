"""Soul assembler — builds the multi-layer system prompt for Atelier.

Layer assembly order:
  1. soul/SOUL.md          — core personality (always attempted)
  2. roles/{user_role}.md  — role-specific instructions
  3. users/{sender_id}.md  — per-user overrides (`:` → `_` in filename)
  4. channels/{channel}_default.md — channel formatting rules
  5. policies/{reply_policy}.md    — active reply-policy overlay
  6. User facts block              — dynamic long-term memory injection

Missing or empty files are silently skipped (logged at DEBUG level).
Layers are joined with "\\n\\n---\\n\\n".
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SEP = "\n\n---\n\n"


def assemble_system_prompt(
    prompts_dir: str | Path,
    channel: str | None = None,
    sender_id: str | None = None,
    user_role: str | None = None,
    reply_policy: str | None = None,
    user_facts: list[str] | None = None,
) -> str:
    """Assemble a multi-layer system prompt from prompt fragments on disk.

    Reads up to six optional layers in a defined order and joins the non-empty
    ones with a horizontal-rule separator.  Missing or empty files are silently
    skipped.  The function never raises for missing files.

    Args:
        prompts_dir: Root directory that contains the soul/, roles/, users/,
            channels/, and policies/ sub-directories.
        channel: Name of the active channel (e.g. ``"telegram"``). When
            provided the file ``channels/{channel}_default.md`` is loaded.
        sender_id: Unique sender identifier (e.g. ``"discord:123456789"``).
            Colons are replaced with underscores when constructing the
            filename so that ``"discord:123"`` maps to
            ``users/discord_123.md``.
        user_role: Role name of the sender (e.g. ``"admin"``).  When provided
            the file ``roles/{user_role}.md`` is loaded.
        reply_policy: Active reply policy key (e.g. ``"in_meeting"``).  When
            provided the file ``policies/{reply_policy}.md`` is loaded.
        user_facts: Optional list of plain-text facts about the user sourced
            from long-term memory.  An empty list is treated as absent.  A
            non-empty list is rendered as a Markdown bullet section appended
            as the final layer.

    Returns:
        A single string containing all present layers joined by
        ``"\\n\\n---\\n\\n"``.  Returns ``""`` when no layer contributes any
        content.
    """
    base = Path(prompts_dir)
    layers: list[str] = []

    # Layer 1 — soul personality
    _append_file(layers, base / "soul" / "SOUL.md", warn_if_missing=True)

    # Layer 2 — role
    if user_role is not None:
        _append_safe(layers, base / "roles", f"{user_role}.md", base)

    # Layer 3 — per-user overrides (sanitize sender_id: `:` → `_`)
    if sender_id is not None:
        safe_id = sender_id.replace(":", "_")
        _append_safe(layers, base / "users", f"{safe_id}.md", base)

    # Layer 4 — channel formatting
    if channel is not None:
        _append_safe(layers, base / "channels", f"{channel}_default.md", base)

    # Layer 5 — reply policy overlay
    if reply_policy is not None:
        _append_safe(layers, base / "policies", f"{reply_policy}.md", base)

    # Layer 6 — user facts from long-term memory
    if user_facts:
        bullet_lines = "\n".join(f"- {fact}" for fact in user_facts)
        layers.append(f"## Mémoire utilisateur\n{bullet_lines}")

    return _SEP.join(layers)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _append_safe(layers: list[str], subdir: Path, filename: str, base: Path) -> None:
    """Resolve *subdir/filename* and append only if it stays inside *base*.

    Prevents path traversal: a crafted ``sender_id`` such as
    ``"discord:../../../etc/passwd"`` would resolve outside *base* and be
    silently dropped.

    Args:
        layers: Accumulator list.
        subdir: Parent directory (e.g. ``base / "users"``).
        filename: Filename to append (already sanitized of `:` → `_`).
        base: Trusted root directory — resolved path must start with this.
    """
    candidate = (subdir / filename).resolve()
    if not str(candidate).startswith(str(base.resolve())):
        logger.warning("Prompt path escapes prompts_dir, skipping: %s", candidate)
        return
    _append_file(layers, candidate)


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
