"""Handler pour l'action ``file_write`` — écriture de fichier mémoire dans SQLite.

Reçu depuis ``relais:memory:request`` avec les champs :
  - ``action``: ``"file_write"``
  - ``user_id``: identifiant unique de l'utilisateur
  - ``path``: chemin virtuel du fichier (ex: ``/memories/notes.md``)
  - ``content``: contenu textuel à écrire
  - ``overwrite``: (bool, optionnel, défaut ``False``) si ``True``, écrase un
    fichier existant
  - ``correlation_id``: UUID pour corréler la réponse

Publie sur ``relais:memory:response`` :
  - ``correlation_id``: repris de la requête
  - ``ok``: ``true`` | ``false``
  - ``error``: message d'erreur ou ``null``
"""

import json
import logging

from souvenir.handlers.base import BaseActionHandler, HandlerContext

logger = logging.getLogger(__name__)

_ALLOWED_PATH_PREFIX = "/memories/"


class FileWriteHandler(BaseActionHandler):
    """Écrit ou crée un fichier mémoire dans SQLite via :class:`FileStore`.

    Supporte deux modes via le champ ``overwrite`` de la requête :

    * ``overwrite=False`` (défaut) : crée un nouveau fichier — retourne une
      erreur si le fichier existe déjà (sémantique *create-only*).
    * ``overwrite=True`` : upsert — crée ou remplace le fichier existant
      (utilisé par l'opération ``edit`` du backend après reconstruction locale
      du contenu).
    """

    async def handle(self, ctx: HandlerContext) -> None:
        """Traite la requête ``file_write``.

        Args:
            ctx: Contexte handler avec ``req``, ``file_store``,
                ``redis_conn`` et ``stream_res``.
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
