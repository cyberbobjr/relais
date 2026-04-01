"""Tool and skills access policy for the Atelier brick.

Centralises the three boundary-sanitisation steps that govern which skills
directories and MCP tools a given request is allowed to use:

1. ``parse_mcp_patterns`` — normalises raw envelope metadata into a tuple of
   fnmatch-style glob patterns.
2. ``resolve_skills``      — expands skill-directory specs into verified
   absolute paths (with path-traversal guard).
3. ``filter_mcp_tools``   — applies the parsed patterns to a list of
   LangChain ``BaseTool`` instances.

All three methods are fail-closed on unexpected input: they return empty
collections rather than raising exceptions, because they operate on
untrusted data from envelope metadata stamped upstream by Portail.
"""

import fnmatch
from pathlib import Path


class ToolPolicy:
    """Encapsulates skill and MCP-tool access policy for a single Atelier instance.

    Instantiate once with the base skills directory; call the methods per
    request to sanitise envelope metadata before it reaches the agentic loop.

    Args:
        base_dir: The root skills directory (resolved from the config cascade
            via ``common.config_loader.resolve_skills_dir()``).
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir: Path = base_dir.resolve()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def parse_mcp_patterns(self, metadata_value: object) -> tuple[str, ...]:
        """Normalise a metadata value into a tuple of MCP-tool glob patterns.

        Accepts a list or tuple of values; returns an empty tuple for any
        other type (None, int, str, …).  This is the boundary where untrusted
        envelope metadata is sanitised before being used for security
        decisions.

        Args:
            metadata_value: The raw value from ``envelope.metadata`` (list,
                tuple, None, …).

        Returns:
            A tuple of strings; never None.
        """
        return self._parse_policy(metadata_value)

    def resolve_skills(self, metadata_value: object) -> list[str]:
        """Parse metadata and expand skill-directory specs into absolute paths.

        Combines ``_parse_policy`` and ``_resolve_paths`` in a single call.
        A ``"*"`` entry expands to all immediate subdirectories of the base
        dir; other entries are resolved relative to the base dir.
        Non-existent paths and path-traversal attempts are silently dropped.

        Args:
            metadata_value: The raw value from ``envelope.metadata``
                (list of directory names, ``["*"]``, None, …).

        Returns:
            List of absolute path strings for directories that exist on disk.
        """
        dirs = self._parse_policy(metadata_value)
        return self._resolve_paths(dirs)

    def filter_mcp_tools(self, tools: list, metadata_value: object) -> list:
        """Parse metadata patterns and return matching tools (fnmatch).

        An empty patterns tuple returns an empty list (fail-closed — no MCP
        access by default).  A ``"*"`` pattern passes every tool.

        Args:
            tools: List of LangChain ``BaseTool`` instances.
            metadata_value: The raw value from ``envelope.metadata``
                (list of glob patterns, ``["*"]``, None, …).

        Returns:
            Filtered list of tools whose names match at least one pattern.
        """
        patterns = self._parse_policy(metadata_value)
        return self._filter_tools(tools, patterns)

    # ------------------------------------------------------------------
    # Private helpers (mirror the three original module-level functions)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_policy(raw: object) -> tuple[str, ...]:
        """Normalise a raw metadata value into a tuple of strings.

        Args:
            raw: Any value from ``envelope.metadata``.

        Returns:
            Tuple of strings; empty tuple on any unexpected type.
        """
        if isinstance(raw, (list, tuple)):
            return tuple(str(v) for v in raw)
        return ()

    def _resolve_paths(self, skills_dirs: tuple[str, ...]) -> list[str]:
        """Expand skill directory specs into existing absolute path strings.

        Args:
            skills_dirs: Tuple of directory names or ``"*"`` wildcard.

        Returns:
            List of absolute path strings for directories that exist on disk.
        """
        if not skills_dirs:
            return []
        resolved: list[str] = []
        for entry in skills_dirs:
            if entry == "*":
                resolved.extend(
                    str(p)
                    for p in sorted(self._base_dir.iterdir())
                    if p.is_dir() and p.is_relative_to(self._base_dir)
                )
            else:
                candidate = (self._base_dir / entry).resolve()
                if candidate.is_dir() and candidate.is_relative_to(self._base_dir):
                    resolved.append(str(candidate))
        return resolved

    @staticmethod
    def _filter_tools(tools: list, patterns: tuple[str, ...]) -> list:
        """Filter tools by fnmatch patterns.

        Args:
            tools: List of LangChain ``BaseTool`` instances.
            patterns: Tuple of fnmatch-style glob patterns.

        Returns:
            Filtered list of tools.
        """
        if not patterns:
            return []
        return [t for t in tools if any(fnmatch.fnmatch(t.name, p) for p in patterns)]
