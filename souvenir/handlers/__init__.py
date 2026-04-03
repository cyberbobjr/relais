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
from souvenir.handlers.file_list_handler import FileListHandler
from souvenir.handlers.file_read_handler import FileReadHandler
from souvenir.handlers.file_write_handler import FileWriteHandler
from souvenir.handlers.get_handler import GetHandler


def build_registry() -> dict[str, BaseActionHandler]:
    """Return a hardcoded mapping of action name → handler instance.

    Returns:
        Dict mapping each supported action string to its handler.
    """
    return {
        "get": GetHandler(),
        "clear": ClearHandler(),
        "file_write": FileWriteHandler(),
        "file_read": FileReadHandler(),
        "file_list": FileListHandler(),
    }


__all__ = ["BaseActionHandler", "HandlerContext", "build_registry"]
