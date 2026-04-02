"""Shared command detection utilities for the RELAIS pipeline.

This module is the single source of truth for deciding whether an incoming
message is a slash command.  It is intentionally free of any business logic
(no ACL, no dispatch) so that both Portail and Sentinelle can import it
without creating circular dependencies.

``KNOWN_COMMANDS`` is derived at import time from
``commandant.commands.KNOWN_COMMANDS``, which is itself derived from the
``COMMAND_REGISTRY`` dict.  Adding or removing a command in
``commandant/commands.py`` automatically propagates to this module.
"""

from __future__ import annotations

from common.text_utils import strip_outer_quotes

# Lazily pull KNOWN_COMMANDS from commandant to avoid circular imports at
# module level.  The import is top-level (not inside a function) so the
# frozenset is created once when this module is first imported.
from commandant.commands import KNOWN_COMMANDS as _commandant_known

KNOWN_COMMANDS: frozenset[str] = frozenset(_commandant_known)


def is_command(content: str) -> bool:
    """Return True if *content* is a slash command.

    A slash command is any message that, after stripping outer whitespace
    and optional symmetric quote characters (``"…"`` or ``'…'``), starts
    with ``'/'`` and contains at least one character after the slash.

    Args:
        content: Raw message text from an incoming envelope.

    Returns:
        ``True`` when the message looks like a command, ``False`` otherwise.

    Raises:
        TypeError: When *content* is not a string.
    """
    stripped = strip_outer_quotes(content)
    return stripped.startswith("/") and len(stripped) > 1


def extract_command_name(content: str) -> str | None:
    """Extract the bare command name from *content*, or ``None`` if not a command.

    The returned name is lowercase and stripped of any arguments.
    Examples::

        extract_command_name("/clear all")  →  "clear"
        extract_command_name("/HELP")       →  "help"
        extract_command_name('"/help"')     →  "help"
        extract_command_name("bonjour")     →  None
        extract_command_name("/")           →  None

    Args:
        content: Raw message text from an incoming envelope.

    Returns:
        Lowercase command name string (no leading slash, no arguments) when
        the content is a valid command syntax; ``None`` otherwise.
    """
    stripped = strip_outer_quotes(content)
    if not stripped.startswith("/") or len(stripped) < 2:
        return None

    # Everything after the leading '/', split on whitespace, first token only
    parts = stripped[1:].split()
    if not parts:
        return None

    return parts[0].lower()
