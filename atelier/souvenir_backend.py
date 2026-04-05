"""Backend DeepAgents qui route les opérations de fichiers vers SOUVENIR via Redis Streams.

Ce module expose :class:`SouvenirBackend`, une implémentation de
``BackendProtocol`` qui persiste les fichiers de mémoire dans la base SQLite
de SOUVENIR plutôt que sur le système de fichiers local.

Protocole Redis
---------------
Requête (stream ``relais:memory:request``) :
    Envelope sérialisée (``Envelope.to_json()``) dont le champ ``metadata``
    contient ``action``, ``user_id``, ``correlation_id`` et tous les kwargs
    propres à chaque action (``path``, ``content``, ``overwrite``, …).

Réponse (stream ``relais:memory:response``) :
    ``{"correlation_id": str, "ok": bool, "error": str|null, ...}``

Thread-safety
-------------
``BackendProtocol`` est synchrone — ses méthodes sont appelées depuis un
thread de pool via ``asyncio.to_thread()``. Ce module utilise donc un client
Redis *synchrone* (``redis.Redis``) isolé, jamais partagé avec le client async
d'Atelier.
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

_STREAM_REQ = "relais:memory:request"
_STREAM_RES = "relais:memory:response"
_TIMEOUT_S = 3.0


class SouvenirBackend(BackendProtocol):
    """Backend DeepAgents qui persiste les fichiers de mémoire dans SOUVENIR.

    Chaque instance est liée à un ``user_id`` unique, transmis dans chaque
    requête Redis de façon à ce que SOUVENIR isole les fichiers par utilisateur.

    Le client Redis synchrone est créé à la première utilisation et réutilisé
    pour toute la durée de vie de l'instance.

    Args:
        user_id: Identifiant unique de l'utilisateur (issu de
            ``envelope.context["portail"]["user_id"]``).
    """

    def __init__(self, user_id: str) -> None:
        """Initialise le backend avec l'identifiant utilisateur.

        Args:
            user_id: Identifiant unique de l'utilisateur.
        """
        self._user_id = user_id
        self._redis: redis_sync.Redis | None = None

    def _get_redis(self) -> redis_sync.Redis:
        """Retourne (et crée si besoin) le client Redis synchrone.

        Utilise la même résolution de chemin socket que :class:`RedisClient` :
        variable d'environnement ``REDIS_SOCKET_PATH`` ou socket par défaut
        dans ``RELAIS_HOME``. Le mot de passe est lu depuis
        ``REDIS_PASS_ATELIER`` (partagé avec le client async d'Atelier).

        Returns:
            Instance ``redis.Redis`` connectée via socket Unix.
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
        """Envoie une requête synchrone à SOUVENIR et attend la réponse.

        Publie dans ``relais:memory:request`` puis poll ``relais:memory:response``
        en filtrant par ``correlation_id`` jusqu'au timeout de 3 s.

        Args:
            action: Nom de l'action (``"file_write"``, ``"file_read"``,
                ``"file_list"``).
            **kwargs: Champs supplémentaires inclus dans le payload JSON.

        Returns:
            Dict de la réponse SOUVENIR, ou ``{"ok": False, "error": "timeout"}``
            si aucune réponse n'arrive dans le délai imparti.
        """
        r = self._get_redis()
        corr = str(uuid.uuid4())

        # Snapshot la queue de réponse AVANT d'envoyer la requête pour ne pas
        # manquer une réponse ultra-rapide ni lire de vieux messages.
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
        """Crée un nouveau fichier mémoire (erreur si le fichier existe déjà).

        Args:
            file_path: Chemin virtuel du fichier (ex: ``/memories/notes.md``).
            content: Contenu textuel à écrire.

        Returns:
            :class:`WriteResult` avec ``files_update=None`` (persistence externe).
        """
        resp = self._request("file_write", path=file_path, content=content, overwrite=False)
        if not resp.get("ok"):
            return WriteResult(error=resp.get("error", "write failed"))
        return WriteResult(path=file_path, files_update=None)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        """Lit le contenu d'un fichier mémoire avec numéros de lignes.

        Args:
            file_path: Chemin virtuel du fichier.
            offset: Numéro de ligne de départ (0-indexé).
            limit: Nombre maximum de lignes à retourner.

        Returns:
            Contenu du fichier formaté avec numéros de lignes (format ``cat -n``),
            ou un message d'erreur si le fichier est introuvable.
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
        """Effectue un remplacement de chaîne dans un fichier mémoire existant.

        Lit le contenu actuel depuis SOUVENIR, applique le remplacement
        localement, puis réécrit le fichier avec ``overwrite=True``.

        Args:
            file_path: Chemin virtuel du fichier.
            old_string: Chaîne exacte à rechercher.
            new_string: Chaîne de remplacement.
            replace_all: Si ``True``, remplace toutes les occurrences.

        Returns:
            :class:`EditResult` avec ``files_update=None`` (persistence externe).
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
        """Liste les fichiers mémoire sous un répertoire virtuel.

        Args:
            path: Chemin virtuel du répertoire (ex: ``/memories/``).

        Returns:
            Liste de :class:`FileInfo` contenant ``path``, ``size`` et
            ``modified_at`` pour chaque fichier.
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
        """Trouve les fichiers mémoire correspondant à un motif glob.

        Liste tous les fichiers sous ``path`` puis filtre par ``pattern``.

        Args:
            pattern: Motif glob (ex: ``*.md``, ``**/*.txt``).
            path: Répertoire de base pour la recherche.

        Returns:
            Liste de :class:`FileInfo` des fichiers correspondants.
        """
        all_files = self.ls_info(path)
        return [f for f in all_files if fnmatch.fnmatch(f["path"], pattern)]

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch] | str:
        """Recherche une chaîne littérale dans les fichiers mémoire.

        Lit chaque fichier depuis SOUVENIR et filtre ligne par ligne.

        Args:
            pattern: Chaîne littérale à rechercher (pas une regex).
            path: Répertoire virtuel de recherche (défaut: ``/memories/``).
            glob: Motif glob pour filtrer les fichiers à inspecter.

        Returns:
            Liste de :class:`GrepMatch` ou message d'erreur string.
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
        """Écrit plusieurs fichiers mémoire (contenu bytes décodé en UTF-8).

        Args:
            files: Liste de tuples ``(path, content_bytes)``.

        Returns:
            Liste de :class:`FileUploadResponse`, une par fichier.
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
        """Lit plusieurs fichiers mémoire.

        Args:
            paths: Liste de chemins virtuels à lire.

        Returns:
            Liste de :class:`FileDownloadResponse`, une par fichier.
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
