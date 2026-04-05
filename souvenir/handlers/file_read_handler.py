"""Handler pour l'action ``file_read`` — lecture d'un fichier mémoire depuis SQLite.

Reçu depuis ``relais:memory:request`` avec les champs :
  - ``action``: ``"file_read"``
  - ``user_id``: identifiant unique de l'utilisateur
  - ``path``: chemin virtuel du fichier (ex: ``/memories/notes.md``)
  - ``correlation_id``: UUID pour corréler la réponse

Publie sur ``relais:memory:response`` :
  - ``correlation_id``: repris de la requête
  - ``ok``: ``true`` | ``false``
  - ``content``: contenu du fichier (string) ou ``null`` si erreur
  - ``error``: message d'erreur ou ``null``
"""

import json
import logging

from souvenir.handlers.base import BaseActionHandler, HandlerContext

logger = logging.getLogger(__name__)

_ALLOWED_PATH_PREFIX = "/memories/"


class FileReadHandler(BaseActionHandler):
    """Lit le contenu d'un fichier mémoire depuis SQLite via :class:`FileStore`."""

    async def handle(self, ctx: HandlerContext) -> None:
        """Traite la requête ``file_read``.

        Args:
            ctx: Contexte handler avec ``req``, ``file_store``,
                ``redis_conn`` et ``stream_res``.
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
