import json
import logging
from typing import Any

logger = logging.getLogger("souvenir.context_store")

_KEY_PREFIX = "relais:context:"


class ContextStore:
    """Stocke l'historique court terme dans Redis List (max 20 msgs, TTL 24h).

    Chaque session est stockée sous la clé ``relais:context:{session_id}``
    sous forme d'une liste JSON de messages compatibles LiteLLM.
    """

    def __init__(
        self,
        redis: Any,
        max_messages: int = 20,
        ttl_seconds: int = 86400,
    ) -> None:
        """Initialise le store.

        Args:
            redis: Connexion Redis async (redis.asyncio.Redis).
            max_messages: Nombre maximum de messages conservés par session.
            ttl_seconds: Durée de vie en secondes (défaut: 24 h).
        """
        self._redis = redis
        self._max_messages = max_messages
        self._ttl_seconds = ttl_seconds

    def _key(self, session_id: str) -> str:
        """Retourne la clé Redis pour la session donnée.

        Args:
            session_id: Identifiant de session.

        Returns:
            Clé Redis sous la forme ``relais:context:{session_id}``.
        """
        return f"{_KEY_PREFIX}{session_id}"

    async def append(self, session_id: str, role: str, content: str) -> None:
        """Ajoute un message à l'historique de la session.

        Utilise RPUSH + LTRIM pour maintenir la fenêtre glissante, puis
        renouvelle le TTL de la clé.

        Args:
            session_id: Identifiant de la session cible.
            role: Rôle du message (``"user"``, ``"assistant"``, etc.).
            content: Contenu textuel du message.
        """
        key = self._key(session_id)
        entry = json.dumps({"role": role, "content": content})
        await self._redis.rpush(key, entry)
        # Garde uniquement les max_messages derniers éléments (index 0-based)
        await self._redis.ltrim(key, -self._max_messages, -1)
        await self._redis.expire(key, self._ttl_seconds)
        logger.debug("Appended message to session %s (role=%s)", session_id, role)

    async def get(self, session_id: str) -> list[dict]:
        """Retourne l'historique formaté pour LiteLLM.

        Args:
            session_id: Identifiant de la session.

        Returns:
            Liste de dicts ``{"role": str, "content": str}`` triés du plus
            ancien au plus récent.
        """
        key = self._key(session_id)
        raw: list[bytes] = await self._redis.lrange(key, 0, -1)
        messages: list[dict] = []
        for item in raw:
            try:
                messages.append(json.loads(item))
            except json.JSONDecodeError as exc:
                logger.warning("Skipping malformed history entry: %s", exc)
        return messages

    async def clear(self, session_id: str) -> None:
        """Efface l'historique d'une session.

        Args:
            session_id: Identifiant de la session à supprimer.
        """
        await self._redis.delete(self._key(session_id))
        logger.debug("Cleared context for session %s", session_id)

    async def get_session_ids(self) -> list[str]:
        """Liste les sessions actives via SCAN (non-blocking).

        Uses an incremental SCAN cursor rather than KEYS to avoid blocking
        the Redis event loop on large keyspaces.

        Returns:
            Liste des session_id actifs.
        """
        pattern = f"{_KEY_PREFIX}*"
        prefix_len = len(_KEY_PREFIX)
        session_ids: list[str] = []
        cursor = 0
        while True:
            cursor, keys = await self._redis.scan(cursor, match=pattern, count=100)
            for k in keys:
                raw = k.decode() if isinstance(k, bytes) else k
                session_ids.append(raw[prefix_len:])
            if cursor == 0:
                break
        return session_ids
