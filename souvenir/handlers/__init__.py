"""Souvenir action handler registry.

Usage::

    from souvenir.handlers import build_registry, HandlerContext

    registry = build_registry()
    handler = registry.get(action)
    if handler:
        await handler.handle(ctx)
"""

from common.envelope_actions import (
    ACTION_MEMORY_ARCHIVE,
    ACTION_MEMORY_CLEAR,
    ACTION_MEMORY_FILE_LIST,
    ACTION_MEMORY_FILE_READ,
    ACTION_MEMORY_FILE_WRITE,
    ACTION_MEMORY_HISTORY_READ,
    ACTION_MEMORY_RESUME,
    ACTION_MEMORY_SESSIONS,
)
from souvenir.handlers.archive_handler import ArchiveHandler
from souvenir.handlers.base import BaseActionHandler, HandlerContext
from souvenir.handlers.clear_handler import ClearHandler
from souvenir.handlers.file_list_handler import FileListHandler
from souvenir.handlers.file_read_handler import FileReadHandler
from souvenir.handlers.file_write_handler import FileWriteHandler
from souvenir.handlers.history_read_handler import HistoryReadHandler
from souvenir.handlers.resume_handler import ResumeHandler
from souvenir.handlers.sessions_handler import SessionsHandler


def build_registry() -> dict[str, BaseActionHandler]:
    """Return a hardcoded mapping of action name → handler instance.

    Returns:
        Dict mapping each supported action string to its handler.
    """
    return {
        ACTION_MEMORY_ARCHIVE: ArchiveHandler(),
        ACTION_MEMORY_CLEAR: ClearHandler(),
        ACTION_MEMORY_FILE_WRITE: FileWriteHandler(),
        ACTION_MEMORY_FILE_READ: FileReadHandler(),
        ACTION_MEMORY_FILE_LIST: FileListHandler(),
        ACTION_MEMORY_SESSIONS: SessionsHandler(),
        ACTION_MEMORY_RESUME: ResumeHandler(),
        ACTION_MEMORY_HISTORY_READ: HistoryReadHandler(),
    }


__all__ = ["BaseActionHandler", "HandlerContext", "build_registry"]
