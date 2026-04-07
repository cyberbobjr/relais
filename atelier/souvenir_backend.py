"""DeepAgents backend that routes file operations to Souvenir via Redis Streams.

This module exposes :class:`SouvenirBackend`, a ``BackendProtocol`` implementation
that persists memory files in Souvenir's SQLite database rather than the local
filesystem.

Redis Protocol
--------------
Request (stream ``relais:memory:request``):
    Serialized Envelope (``Envelope.to_json()``).  The ``action`` is carried in
    the ``Envelope.action`` field; action parameters (``user_id``, ``path``,
    ``content``, ``overwrite``, …) are in ``context["souvenir_request"]``
    (``CTX_SOUVENIR_REQUEST``).

Response (stream ``relais:memory:response``):
    ``{"correlation_id": str, "ok": bool, "error": str|null, ...}``

Thread-safety
-------------
``BackendProtocol`` is synchronous — its methods are called from a thread pool
via ``asyncio.to_thread()``. This module therefore uses an isolated *synchronous*
Redis client (``redis.Redis``), never shared with Atelier's async client.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import time
import uuid
from typing import Any

import os

import redis as redis_sync

from common.config_loader import get_relais_home
from common.contexts import CTX_SOUVENIR_REQUEST
from common.envelope import Envelope
from common.streams import STREAM_MEMORY_REQUEST, STREAM_MEMORY_RESPONSE
from deepagents.backends import BackendProtocol
from deepagents.backends.protocol import (
    EditResult,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GrepMatch,
    WriteResult,
)

logger = logging.getLogger(__name__)

_STREAM_REQ = STREAM_MEMORY_REQUEST
_STREAM_RES = STREAM_MEMORY_RESPONSE
_TIMEOUT_S = 3.0


class SouvenirBackend(BackendProtocol):
    """DeepAgents backend that persists memory files in Souvenir.

    Each instance is bound to a unique ``user_id``, transmitted in every Redis
    request so that Souvenir isolates files per user.

    The synchronous Redis client is created on first use and reused for the
    lifetime of the instance.

    Args:
        user_id: Unique user identifier (sourced from
            ``envelope.context["portail"]["user_id"]``).
    """

    def __init__(self, user_id: str) -> None:
        """Initialise the backend with the user identifier.

        Args:
            user_id: Unique user identifier.
        """
        self._user_id = user_id
        self._redis: redis_sync.Redis | None = None

    def _get_redis(self) -> redis_sync.Redis:
        """Return (and create if needed) the synchronous Redis client.

        Uses the same socket path resolution as :class:`RedisClient`:
        the ``REDIS_SOCKET_PATH`` environment variable or the default socket
        path under ``RELAIS_HOME``. The password is read from
        ``REDIS_PASS_ATELIER`` (shared with Atelier's async client).

        Returns:
            A ``redis.Redis`` instance connected via Unix socket.
        """
        if self._redis is None:
            default_socket = str(get_relais_home() / "redis.sock")
            socket_path = os.environ.get("REDIS_SOCKET_PATH", default_socket)
            password = os.environ.get("REDIS_PASS_ATELIER") or os.environ.get("REDIS_PASSWORD")
            self._redis = redis_sync.Redis(
                unix_socket_path=socket_path,
                username="atelier",
                password=password,
                decode_responses=False,
            )
        return self._redis

    def _request(self, action: str, **kwargs: Any) -> dict[str, Any]:
        """Send a synchronous request to Souvenir and wait for the response.

        Publishes to ``relais:memory:request`` then polls ``relais:memory:response``
        filtering by ``correlation_id`` until the 3-second timeout.

        Args:
            action: Action name (``"file_write"``, ``"file_read"``,
                ``"file_list"``).
            **kwargs: Additional fields included in the JSON payload.

        Returns:
            Souvenir response dict, or ``{"ok": False, "error": "timeout"}``
            if no response arrives within the allotted time.
        """
        r = self._get_redis()
        corr = str(uuid.uuid4())

        # Snapshot the response queue BEFORE sending the request so we don't
        # miss an ultra-fast response or read stale messages.
        last_msgs = r.xrevrange(_STREAM_RES, count=1)
        last_id: str = last_msgs[0][0].decode() if last_msgs else "0-0"

        # Publish an Envelope so Souvenir can consume via BrickBase._run_stream_loop.
        # The action is set as first-class field; parameters go in CTX_SOUVENIR_REQUEST.
        envelope = Envelope(
            content="",
            sender_id=f"atelier:{self._user_id}",
            channel="internal",
            session_id="",
            correlation_id=corr,
            action=action,
            context={CTX_SOUVENIR_REQUEST: {"user_id": self._user_id, **kwargs}},
        )
        r.xadd(_STREAM_REQ, {"payload": envelope.to_json()})

        deadline = time.monotonic() + _TIMEOUT_S
        while time.monotonic() < deadline:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            msgs = r.xread({_STREAM_RES: last_id}, count=50, block=min(remaining_ms, 500))
            if not msgs:
                continue
            for _stream, messages in msgs:
                for msg_id_raw, data in messages:
                    raw_bytes = data.get(b"payload", b"{}")
                    resp: dict[str, Any] = json.loads(raw_bytes.decode())
                    msg_id = msg_id_raw.decode() if isinstance(msg_id_raw, bytes) else msg_id_raw
                    last_id = msg_id
                    if resp.get("correlation_id") == corr:
                        return resp

        logger.warning(
            "SouvenirBackend timeout waiting for action=%s user=%s corr=%s",
            action,
            self._user_id,
            corr,
        )
        return {"ok": False, "error": "timeout"}

    # ------------------------------------------------------------------
    # BackendProtocol — file operations
    # ------------------------------------------------------------------

    def write(self, file_path: str, content: str) -> WriteResult:
        """Create a new memory file (error if the file already exists).

        Args:
            file_path: Virtual file path (e.g. ``/memories/notes.md``).
            content: Text content to write.

        Returns:
            :class:`WriteResult` with ``files_update=None`` (external persistence).
        """
        resp = self._request("file_write", path=file_path, content=content, overwrite=False)
        if not resp.get("ok"):
            return WriteResult(error=resp.get("error", "write failed"))
        return WriteResult(path=file_path, files_update=None)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read the content of a memory file with line numbers.

        Args:
            file_path: Virtual file path.
            offset: Starting line number (0-indexed).
            limit: Maximum number of lines to return.

        Returns:
            File content formatted with line numbers (``cat -n`` format),
            or an error message if the file is not found.
        """
        resp = self._request("file_read", path=file_path)
        if not resp.get("ok"):
            return f"Error: {resp.get('error', 'read failed')}"
        raw: str = resp.get("content") or ""
        lines = raw.splitlines()
        sliced = lines[offset: offset + limit]
        return "\n".join(f"{offset + i + 1}\t{line}" for i, line in enumerate(sliced))

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Perform a string replacement in an existing memory file.

        Reads the current content from Souvenir, applies the replacement
        locally, then rewrites the file with ``overwrite=True``.

        Args:
            file_path: Virtual file path.
            old_string: Exact string to search for.
            new_string: Replacement string.
            replace_all: If ``True``, replaces all occurrences.

        Returns:
            :class:`EditResult` with ``files_update=None`` (external persistence).
        """
        read_resp = self._request("file_read", path=file_path)
        if not read_resp.get("ok"):
            return EditResult(error=read_resp.get("error", "file not found"))

        current: str = read_resp.get("content") or ""
        if old_string not in current:
            return EditResult(error=f"old_string not found in {file_path}")

        if replace_all:
            updated = current.replace(old_string, new_string)
            occurrences = current.count(old_string)
        else:
            count = current.count(old_string)
            if count > 1:
                return EditResult(error=f"old_string is not unique in {file_path} ({count} occurrences). Use replace_all=True or provide more context.")
            updated = current.replace(old_string, new_string, 1)
            occurrences = 1

        write_resp = self._request("file_write", path=file_path, content=updated, overwrite=True)
        if not write_resp.get("ok"):
            return EditResult(error=write_resp.get("error", "write failed"))
        return EditResult(path=file_path, occurrences=occurrences, files_update=None)

    def ls_info(self, path: str) -> list[FileInfo]:
        """List memory files under a virtual directory.

        Args:
            path: Virtual directory path (e.g. ``/memories/``).

        Returns:
            List of :class:`FileInfo` containing ``path``, ``size`` and
            ``modified_at`` for each file.
        """
        prefix = path if path.endswith("/") else path + "/"
        resp = self._request("file_list", path=prefix)
        if not resp.get("ok"):
            return []
        files: list[dict[str, Any]] = resp.get("files") or []
        return [
            FileInfo(
                path=f["path"],
                is_dir=False,
                size=f.get("size", 0),
                modified_at=f.get("modified_at", ""),
            )
            for f in files
        ]

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        """Find memory files matching a glob pattern.

        Lists all files under ``path`` then filters by ``pattern``.

        Args:
            pattern: Glob pattern (e.g. ``*.md``, ``**/*.txt``).
            path: Base directory for the search.

        Returns:
            List of :class:`FileInfo` for matching files.
        """
        all_files = self.ls_info(path)
        return [f for f in all_files if fnmatch.fnmatch(f["path"], pattern)]

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch] | str:
        """Search for a literal string in memory files.

        Reads each file from Souvenir and filters line by line.

        Args:
            pattern: Literal string to search for (not a regex).
            path: Virtual search directory (default: ``/memories/``).
            glob: Glob pattern to filter which files to inspect.

        Returns:
            List of :class:`GrepMatch` or an error message string.
        """
        search_path = path or "/memories/"
        candidates = self.ls_info(search_path)
        if glob:
            candidates = [f for f in candidates if fnmatch.fnmatch(f["path"], glob)]

        matches: list[GrepMatch] = []
        for file_info in candidates:
            read_resp = self._request("file_read", path=file_info["path"])
            if not read_resp.get("ok"):
                continue
            content: str = read_resp.get("content") or ""
            for line_no, line in enumerate(content.splitlines(), start=1):
                if pattern in line:
                    matches.append(GrepMatch(path=file_info["path"], line=line_no, text=line))
        return matches

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Write multiple memory files (bytes content decoded as UTF-8).

        Args:
            files: List of ``(path, content_bytes)`` tuples.

        Returns:
            List of :class:`FileUploadResponse`, one per file.
        """
        results: list[FileUploadResponse] = []
        for fpath, content_bytes in files:
            try:
                content = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                results.append(FileUploadResponse(path=fpath, error="invalid_path"))
                continue
            resp = self._request("file_write", path=fpath, content=content, overwrite=True)
            if resp.get("ok"):
                results.append(FileUploadResponse(path=fpath, error=None))
            else:
                results.append(FileUploadResponse(path=fpath, error="permission_denied"))
        return results

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Read multiple memory files.

        Args:
            paths: List of virtual paths to read.

        Returns:
            List of :class:`FileDownloadResponse`, one per file.
        """
        results: list[FileDownloadResponse] = []
        for fpath in paths:
            resp = self._request("file_read", path=fpath)
            if resp.get("ok"):
                content: str = resp.get("content") or ""
                results.append(FileDownloadResponse(path=fpath, content=content.encode("utf-8"), error=None))
            else:
                results.append(FileDownloadResponse(path=fpath, content=None, error="file_not_found"))
        return results
