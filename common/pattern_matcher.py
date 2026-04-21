"""Shared fnmatch pattern-matching utilities for the RELAIS pipeline.

Provides two pure functions used across multiple bricks (Atelier subagent
registry, ToolPolicy, subagents resolver) to normalise raw pattern specs
and apply fnmatch filtering.  Centralised here to eliminate duplication
and ensure consistent fail-closed semantics.

Fail-closed contract:
- ``parse_patterns`` with no meaningful input returns an empty tuple.
- ``matches`` with an empty patterns tuple returns ``False`` (nothing is
  authorised by default).
"""

from __future__ import annotations

from fnmatch import fnmatch


def parse_patterns(raw: list[str] | tuple[str, ...] | str | None) -> tuple[str, ...]:
    """Normalise a raw pattern spec into an immutable tuple of strings.

    Accepted input types and their results:

    - ``None`` → ``()``
    - ``[]`` / ``()`` → ``()``
    - ``"glob-*"`` (bare string) → ``("glob-*",)``
    - ``["foo", "bar-*"]`` → ``("foo", "bar-*")``
    - ``("foo", "bar-*")`` → ``("foo", "bar-*")``

    Any other type (int, dict, …) is treated as ``None`` and returns ``()``.

    Args:
        raw: The raw pattern value — a string, a list/tuple of strings, or
            None.

    Returns:
        An immutable tuple of pattern strings ready for use with
        ``matches()``.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw)
    return ()


def matches(name: str, patterns: tuple[str, ...]) -> bool:
    """Return True if *name* matches at least one fnmatch pattern.

    Fail-closed: an empty *patterns* tuple always returns ``False``,
    meaning no access is granted when no patterns are configured.

    Args:
        name: The string to test (e.g. a subagent name or tool name).
        patterns: Immutable tuple of fnmatch-style glob patterns as
            returned by ``parse_patterns()``.

    Returns:
        ``True`` if at least one pattern matches *name*, ``False``
        otherwise (including when *patterns* is empty).
    """
    return any(fnmatch(name, p) for p in patterns)
