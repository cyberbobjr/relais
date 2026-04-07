"""Handler for the ``file_list`` action — listing memory files from SQLite.

Received from ``relais:memory:request`` with fields:
  - ``action``: ``"file_list"``
  - ``user_id``: unique user identifier
  - ``path``: directory prefix to list (e.g. ``/memories/``)
  - ``correlation_id``: UUID to correlate the response

Publishes to ``relais:memory:response``:
  - ``correlation_id``: echoed from the request
  - ``ok``: ``true`` | ``false``
  - ``files``: list of dicts ``{"path": str, "size": int, "modified_at": str}``
    or ``null`` on error
  - ``error``: error message or ``null``
"""

import json
import logging

from souvenir.handlers.base import BaseActionHandler, HandlerContext

logger = logging.getLogger(__name__)

_ALLOWED_PATH_PREFIX = "/memories/"


class FileListHandler(BaseActionHandler):
    """List memory files for a user under a path prefix."""

    async def handle(self, ctx: HandlerContext) -> None:
        """Process a ``file_list`` request.

        Args:
            ctx: Handler context with ``req``, ``file_store``,
                ``redis_conn`` and ``stream_res``.
        """
        req = ctx.req
        corr = req.get("correlation_id", "")
        user_id: str = req.get("user_id", "")
        path: str = req.get("path", "/memories/")

        if not path.startswith(_ALLOWED_PATH_PREFIX):
            logger.error(
                "Souvenir: rejected file operation — path %r does not start with %r",
                path,
                _ALLOWED_PATH_PREFIX,
            )
            await ctx.redis_conn.xadd(
                ctx.stream_res,
                {"payload": json.dumps({"correlation_id": corr, "ok": False, "files": None, "error": "Invalid path prefix"})},
            )
            return

        if not user_id:
            await ctx.redis_conn.xadd(
                ctx.stream_res,
                {"payload": json.dumps({"correlation_id": corr, "ok": False, "files": None, "error": "Missing user_id"})},
            )
            return

        files = await ctx.file_store.list_files(user_id=user_id, path_prefix=path)
        logger.debug("file_list ok user=%s path=%s count=%d", user_id, path, len(files))
        payload = {"correlation_id": corr, "ok": True, "files": files, "error": None}
        await ctx.redis_conn.xadd(ctx.stream_res, {"payload": json.dumps(payload)})
