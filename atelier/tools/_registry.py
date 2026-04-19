"""Static tool registry for Atelier.

Discovers ``@tool``-decorated functions (and any other ``BaseTool``
instances) from Python modules under ``atelier/tools/``.

At decoration time, LangChain's ``@tool`` decorator wraps the function in
a ``StructuredTool`` subclass, so every decorated function becomes a
``BaseTool`` instance available as a module-level attribute.  This
registry collects them by scanning the package with ``pkgutil.iter_modules``.

Modules whose names start with ``_`` (e.g. ``_registry.py``) are skipped
by convention so internal helpers are never exposed to the agentic loop.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool

from common.config_loader import resolve_bundles_dir

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolRegistry:
    """Immutable registry of static ``@tool``-decorated functions.

    Built once at ``Atelier.__init__`` time via ``discover()``, then
    queried per-request to resolve tool tokens in subagent specs.

    Attributes:
        _tools: Mapping from tool name to ``BaseTool`` instance.
    """

    _tools: dict[str, BaseTool]

    @classmethod
    def discover(cls) -> ToolRegistry:
        """Scan ``atelier.tools`` and collect all ``BaseTool`` instances.

        Iterates over every module in the ``atelier.tools`` package using
        ``pkgutil.iter_modules``.  Modules whose names start with ``_``
        are skipped (internal convention).  Each non-underscore module is
        imported and its attributes are inspected; any attribute that is
        an instance of ``BaseTool`` is added to the registry under its
        ``.name``.

        Import failures are logged as warnings and the module is skipped
        (fail-open for startup — no single broken tool prevents the
        registry from being populated with other valid tools).

        Returns:
            A frozen ``ToolRegistry`` containing all discovered tools.
        """
        import atelier.tools as package
        from atelier.subagents_resolver import _load_tools_from_module

        tools: dict[str, BaseTool] = {}
        for finder, name, _ispkg in pkgutil.iter_modules(package.__path__):
            if name.startswith("_"):
                continue
            fqn = f"atelier.tools.{name}"
            try:
                mod = importlib.import_module(fqn)
            except Exception:
                logger.warning(
                    "ToolRegistry: failed to import module %s", fqn, exc_info=True
                )
                continue
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name, None)
                if isinstance(obj, BaseTool):
                    logger.debug(
                        "ToolRegistry: registered tool %r from %s", obj.name, fqn
                    )
                    tools[obj.name] = obj

        # Scan installed bundles: ~/.relais/bundles/*/tools/*.py
        bundles_dir: Path = resolve_bundles_dir()
        if bundles_dir.is_dir():
            for bundle_dir in sorted(bundles_dir.iterdir()):
                if not bundle_dir.is_dir():
                    continue
                bundle_name = bundle_dir.name
                bundle_tools_dir = bundle_dir / "tools"
                if not bundle_tools_dir.is_dir():
                    continue
                for py_file in sorted(bundle_tools_dir.glob("*.py")):
                    loaded = _load_tools_from_module(py_file, f"bundle:{bundle_name}")
                    for tool_name, tool_obj in loaded.items():
                        if tool_name in tools:
                            logger.warning(
                                "ToolRegistry: bundle tool name conflict — '%s' from "
                                "bundle '%s' replaces previous registration (last-wins)",
                                tool_name,
                                bundle_name,
                            )
                        tool_obj._bundle_name = bundle_name  # type: ignore[attr-defined]
                        tools[tool_name] = tool_obj
                        logger.debug(
                            "ToolRegistry: registered bundle tool %r from %s/%s",
                            tool_name,
                            bundle_name,
                            py_file.name,
                        )

        logger.info("ToolRegistry: discovered %d tool(s)", len(tools))
        return cls(_tools=tools)

    def get(self, name: str) -> BaseTool | None:
        """Return the tool registered under *name*, or None.

        Args:
            name: The tool name to look up.

        Returns:
            The ``BaseTool`` instance if registered, otherwise ``None``.
        """
        return self._tools.get(name)

    def all(self) -> dict[str, BaseTool]:
        """Return all registered tools as a name → tool mapping.

        Returns:
            A dict mapping tool name strings to ``BaseTool`` instances.
        """
        return dict(self._tools)
