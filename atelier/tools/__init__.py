"""Static tool registry package for the Atelier brick.

Modules in this package (except those starting with ``_``) are
auto-discovered by ``ToolRegistry.discover()`` at Atelier startup.
Each module-level attribute that is a ``langchain_core.tools.BaseTool``
instance (including ``@tool``-decorated functions, which become
``StructuredTool`` instances at decoration time) is registered.

Usage::

    from atelier.tools._registry import ToolRegistry

    registry = ToolRegistry.discover()
    tool = registry.get("my_tool")
"""
