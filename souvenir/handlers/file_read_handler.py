"""Handler for the ``file_read`` action — reading a memory file from SQLite.

Received from ``relais:memory:request`` with fields:
  - ``action``: ``"file_read"``
  - ``user_id``: unique user identifier
  - ``path``: virtual file path (e.g. ``/memories/notes.md``)
  - ``correlation_id``: UUID to correlate the response

Publishes to ``relais:memory:response``:
  - ``correlation_id``: echoed from the request
  - ``ok``: ``true`` | ``false``
  - ``content``: file content (string) or ``null`` on error
  - ``error``: error message or ``null``
"""

import json
import logging

from souvenir.handlers.base import BaseActionHandler, HandlerContext

logger = logging.getLogger(__name__)

_ALLOWED_PATH_PREFIX = "/memories/"


class FileReadHandler(BaseActionHandler):
    """Read a memory file from SQLite via :class:`FileStore`."""

    async def handle(self, ctx: HandlerContext) -> None:
        """Process a ``file_read`` request.

        Args:
            ctx: Handler context with ``req``, ``file_store``,
                ``redis_conn`` and ``stream_res``.
        """
        req = ctx.req
        corr = req.get("correlation_id", "")
        user_id: str = req.get("user_id", "")
        path: str = req.get("path", "")

        if not path.startswith(_ALLOWED_PATH_PREFIX):
            logger.error(
                "Souvenir: rejected file operation — path %r does not start with %r",
                path,
                _ALLOWED_PATH_PREFIX,
            )
            await ctx.redis_conn.xadd(
                ctx.stream_res,
                {"payload": json.dumps({"correlation_id": corr, "ok": False, "content": None, "error": "Invalid path prefix"})},
            )
            return

        if not user_id or not path:
            await ctx.redis_conn.xadd(
                ctx.stream_res,
                {"payload": json.dumps({"correlation_id": corr, "ok": False, "content": None, "error": "Missing user_id or path"})},
            )
            return

        content, error = await ctx.file_store.read_file(user_id=user_id, path=path)

        if error:
            logger.debug("file_read not found user=%s path=%s", user_id, path)
            payload = {"correlation_id": corr, "ok": False, "content": None, "error": error}
        else:
            logger.debug("file_read ok user=%s path=%s", user_id, path)
            payload = {"correlation_id": corr, "ok": True, "content": content, "error": None}

        await ctx.redis_conn.xadd(ctx.stream_res, {"payload": json.dumps(payload)})
