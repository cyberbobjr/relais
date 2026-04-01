"""Souvenir action handler registry.

Usage::

    from souvenir.handlers import build_registry, HandlerContext

    registry = build_registry()
    handler = registry.get(action)
    if handler:
        await handler.handle(ctx)
"""

from souvenir.handlers.base import BaseActionHandler, HandlerContext
from souvenir.handlers.clear_handler import ClearHandler
from souvenir.handlers.get_handler import GetHandler
from souvenir.handlers.store_memory_handler import StoreMemoryHandler


def build_registry() -> dict[str, BaseActionHandler]:
    """Return a hardcoded mapping of action name → handler instance.

    Returns:
        Dict mapping each supported action string to its handler.
    """
    return {
        "get": GetHandler(),
        "clear": ClearHandler(),
        "store_memory": StoreMemoryHandler(),
    }


__all__ = ["BaseActionHandler", "HandlerContext", "build_registry"]
