import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("souvenir.context_store")

_KEY_PREFIX = "relais:context:"

# Callable type alias for the async LLM summarisation client.
# Receives a list of message dicts and returns a summary string.
LLMClient = Callable[[list[dict]], Awaitable[str]]


class ContextStore:
    """Stocke l'historique court terme dans Redis List (max 20 msgs, TTL 24h).

    Chaque session est stockée sous la clé ``relais:context:{session_id}``
    sous forme d'une liste JSON de messages compatibles LiteLLM.

    When an ``llm_client`` is provided, ``append()`` automatically triggers
    context window compaction once the list reaches 80 % of ``max_messages``.
    """

    def __init__(
        self,
        redis: Any,
        max_messages: int = 20,
        ttl_seconds: int = 86400,
        llm_client: LLMClient | None = None,
    ) -> None:
        """Initialise le store.

        Args:
            redis: Connexion Redis async (redis.asyncio.Redis).
            max_messages: Nombre maximum de messages conservés par session.
            ttl_seconds: Durée de vie en secondes (défaut: 24 h).
            llm_client: Optional async callable ``async fn(messages) -> str``
                used for context compaction.  When ``None`` (default) no
                compaction is performed and existing behaviour is preserved.
        """
        self._redis = redis
        self._max_messages = max_messages
        self._ttl_seconds = ttl_seconds
        self._llm_client = llm_client

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
        # TODO : pourquoi une fenêtre glissante si l'idée est d'avoir un historique complet de la session ?
        await self._redis.ltrim(key, -self._max_messages, -1)
        await self._redis.expire(key, self._ttl_seconds)
        logger.debug("Appended message to session %s (role=%s)", session_id, role)
        if self._llm_client is not None:
            await self.maybe_compact(session_id, llm_client=self._llm_client)

    async def maybe_compact(
        self,
        user_id: str,
        llm_client: LLMClient | None = None,
    ) -> bool:
        """Trigger compaction if context is at 80%+ capacity.

        Compacts the oldest messages into a single summary message using the
        configured LLM.  The summary replaces the first half of the list while
        the second half is preserved verbatim so the most recent exchanges
        remain immediately available to the LLM.

        Compaction algorithm:
          1. LLEN the Redis list.
          2. If count < threshold (80 % of max_messages), return False early.
          3. LRANGE 0 -1 to load all messages.
          4. Split: ``to_compact = messages[:count//2]``,
             ``to_keep = messages[count//2:]``.
          5. Call ``llm_client(to_compact)`` to obtain a summary string.
          6. Build a summary message: ``{"role": "system", "content":
             "[RÉSUMÉ] {summary}", "timestamp": <epoch>}``.
          7. Atomically rebuild the list: DEL + RPUSH(summary, *to_keep).
          8. Reset TTL.
          9. Return True.

        If the LLM call raises, a warning is logged, the list is left
        unchanged, and the method returns False (non-fatal).

        Args:
            user_id: The user whose context to potentially compact.  The Redis
                key is derived as ``relais:context:{user_id}``.
            llm_client: Optional async callable ``async fn(messages) -> str``.
                Falls back to ``self._llm_client`` when omitted.  If neither
                is set, raises ``ValueError``.

        Returns:
            True if compaction was triggered and completed successfully,
            False otherwise (below threshold or LLM failure).

        Raises:
            ValueError: If no LLM client is available (neither argument nor
                instance attribute).
        """
        client = llm_client or self._llm_client
        if client is None:
            raise ValueError(
                "maybe_compact() requires an llm_client but none was provided."
            )

        threshold = int(self._max_messages * 0.8)
        key = self._key(user_id)

        count: int = await self._redis.llen(key)
        if count < threshold:
            return False

        raw: list[bytes] = await self._redis.lrange(key, 0, -1)
        messages: list[dict] = []
        for item in raw:
            try:
                messages.append(json.loads(item))
            except json.JSONDecodeError:
                pass

        split_at = len(messages) // 2
        to_compact = messages[:split_at]
        to_keep = messages[split_at:]

        try:
            summary: str = await client(to_compact)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Context compaction failed for user %s — leaving context unchanged: %s",
                user_id,
                exc,
            )
            return False

        summary_msg = json.dumps(
            {
                "role": "system",
                "content": f"[RÉSUMÉ] {summary}",
                "timestamp": time.time(),
            }
        )
        rebuilt = [summary_msg] + [json.dumps(m) for m in to_keep]

        await self._redis.delete(key)
        await self._redis.rpush(key, *rebuilt)
        await self._redis.expire(key, self._ttl_seconds)

        logger.debug(
            "Compacted context for user %s: %d → %d messages",
            user_id,
            len(messages),
            len(rebuilt),
        )
        return True

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

    async def get_recent(self, session_id: str, limit: int = 20) -> list[dict]:
        """Retourne les ``limit`` derniers tours depuis la liste Redis.

        Utilise LRANGE avec des indices négatifs pour obtenir la fin de la
        liste sans charger l'intégralité des entrées.

        Args:
            session_id: Identifiant de la session.
            limit: Nombre maximum de messages à retourner (défaut: 20).

        Returns:
            Liste de dicts ``{"role": str, "content": str}`` du plus ancien au
            plus récent. Retourne ``[]`` si la session n'existe pas.
        """
        key = self._key(session_id)
        raw: list[bytes] = await self._redis.lrange(key, -limit, -1)
        result: list[dict] = []
        for item in raw:
            try:
                result.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                pass
        return result

    async def append_turn(
        self, session_id: str, user_content: str, assistant_content: str
    ) -> None:
        """Ajoute une paire utilisateur/assistant à l'historique de la session.

        Pousse les deux messages en un seul RPUSH, trim la liste à ``_max_messages``
        éléments et renouvelle le TTL.

        Args:
            session_id: Identifiant de la session cible.
            user_content: Contenu du message utilisateur.
            assistant_content: Contenu de la réponse de l'assistant.
        """
        key = self._key(session_id)
        user_turn = json.dumps({"role": "user", "content": user_content})
        assistant_turn = json.dumps({"role": "assistant", "content": assistant_content})
        await self._redis.rpush(key, user_turn, assistant_turn)
        await self._redis.ltrim(key, -self._max_messages, -1)
        await self._redis.expire(key, self._ttl_seconds)
        logger.debug("Appended turn to session %s", session_id)

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
