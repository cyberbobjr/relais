"""Handler for the ``file_write`` action — writing a memory file to SQLite.

Received from ``relais:memory:request`` with fields:
  - ``action``: ``"file_write"``
  - ``user_id``: unique user identifier
  - ``path``: virtual file path (e.g. ``/memories/notes.md``)
  - ``content``: text content to write
  - ``overwrite``: (bool, optional, default ``False``) if ``True``, overwrites
    an existing file
  - ``correlation_id``: UUID to correlate the response

Publishes to ``relais:memory:response``:
  - ``correlation_id``: echoed from the request
  - ``ok``: ``true`` | ``false``
  - ``error``: error message or ``null``
"""

import json
import logging

from souvenir.handlers.base import BaseActionHandler, HandlerContext

logger = logging.getLogger(__name__)

_ALLOWED_PATH_PREFIX = "/memories/"


class FileWriteHandler(BaseActionHandler):
    """Write or create a memory file in SQLite via :class:`FileStore`.

    Supports two modes via the ``overwrite`` request field:

    * ``overwrite=False`` (default): create a new file — returns an error if
      the file already exists (*create-only* semantics).
    * ``overwrite=True``: upsert — creates or replaces the existing file
      (used by the backend ``edit`` operation after local content reconstruction).
    """

    async def handle(self, ctx: HandlerContext) -> None:
        """Process a ``file_write`` request.

        Args:
            ctx: Handler context with ``req``, ``file_store``,
                ``redis_conn`` and ``stream_res``.
        """
        req = ctx.req
        corr = req.get("correlation_id", "")
        user_id: str = req.get("user_id", "")
        path: str = req.get("path", "")
        content: str = req.get("content", "")
        overwrite: bool = bool(req.get("overwrite", False))

        if not path.startswith(_ALLOWED_PATH_PREFIX):
            logger.error(
                "Souvenir: rejected file operation — path %r does not start with %r",
                path,
                _ALLOWED_PATH_PREFIX,
            )
            await ctx.redis_conn.xadd(
                ctx.stream_res,
                {"payload": json.dumps({"correlation_id": corr, "ok": False, "error": "Invalid path prefix"})},
            )
            return

        if not user_id or not path:
            await ctx.redis_conn.xadd(
                ctx.stream_res,
                {"payload": json.dumps({"correlation_id": corr, "ok": False, "error": "Missing user_id or path"})},
            )
            return

        error = await ctx.file_store.write_file(
            user_id=user_id,
            path=path,
            content=content,
            overwrite=overwrite,
        )

        if error:
            logger.debug("file_write failed user=%s path=%s: %s", user_id, path, error)
            payload = {"correlation_id": corr, "ok": False, "error": error}
        else:
            logger.debug("file_write ok user=%s path=%s overwrite=%s", user_id, path, overwrite)
            payload = {"correlation_id": corr, "ok": True, "error": None}

        await ctx.redis_conn.xadd(ctx.stream_res, {"payload": json.dumps(payload)})
