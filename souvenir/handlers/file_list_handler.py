"""Handler pour l'action ``file_list`` — listage des fichiers mémoire depuis SQLite.

Reçu depuis ``relais:memory:request`` avec les champs :
  - ``action``: ``"file_list"``
  - ``user_id``: identifiant unique de l'utilisateur
  - ``path``: préfixe de répertoire à lister (ex: ``/memories/``)
  - ``correlation_id``: UUID pour corréler la réponse

Publie sur ``relais:memory:response`` :
  - ``correlation_id``: repris de la requête
  - ``ok``: ``true`` | ``false``
  - ``files``: liste de dicts ``{"path": str, "size": int, "modified_at": str}``
    ou ``null`` si erreur
  - ``error``: message d'erreur ou ``null``
"""

import json
import logging

from souvenir.handlers.base import BaseActionHandler, HandlerContext

logger = logging.getLogger(__name__)

_ALLOWED_PATH_PREFIX = "/memories/"


class FileListHandler(BaseActionHandler):
    """Liste les fichiers mémoire d'un utilisateur sous un préfixe de chemin."""

    async def handle(self, ctx: HandlerContext) -> None:
        """Traite la requête ``file_list``.

        Args:
            ctx: Contexte handler avec ``req``, ``file_store``,
                ``redis_conn`` et ``stream_res``.
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
