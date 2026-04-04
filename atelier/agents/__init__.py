"""Subagent plugin package for the Atelier brick.

Each Python module in this package (except those starting with ``_``)
is auto-discovered by ``SubagentRegistry.discover()`` at Atelier startup.

Usage::

    from atelier.agents import SubagentRegistry

    registry = SubagentRegistry.discover()
    specs = registry.specs_for_user(user_record)
"""

from atelier.agents._registry import SubagentRegistry

__all__ = ["SubagentRegistry"]
