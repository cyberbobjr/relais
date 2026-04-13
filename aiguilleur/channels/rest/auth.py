"""Bearer token authentication middleware for the REST channel adapter.

Validates the ``Authorization: Bearer <token>`` header against the
``UserRegistry``. Stores ``request["user_record"]`` and
``request["sender_id"]`` on success so downstream handlers can read the
resolved user without re-querying the registry.

Security rules enforced here:
- The raw token is NEVER written to logs (only its length).
- ``sender_id`` uses the stable ``user_id`` from portail.yaml — the raw
  token is never propagated into the Redis pipeline or stored in memory.
- Blocked users are rejected with 401 even if their token is valid.
- Missing, malformed, or unrecognised tokens all return 401 (no info leak).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Awaitable

from aiohttp import web

if TYPE_CHECKING:
    from portail.user_registry import UserRegistry

logger = logging.getLogger("aiguilleur.rest.auth")


def make_bearer_auth_middleware(registry: "UserRegistry"):
    """Create a new-style aiohttp @web.middleware function for Bearer auth.

    This factory wraps the registry in a closure and returns a proper
    ``@web.middleware``-decorated coroutine, which avoids the
    "old-style middleware deprecated" warning from aiohttp >= 3.x.

    Args:
        registry: User registry for API key resolution.

    Returns:
        An aiohttp middleware coroutine.
    """
    @web.middleware
    async def bearer_auth_middleware(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.Response]],
    ) -> web.Response:
        return await _check_bearer(registry, request, handler)

    return bearer_auth_middleware


async def _check_bearer(
    registry: "UserRegistry",
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.Response]],
) -> web.Response:
    """Core auth logic: validate Bearer token and delegate to handler.

    Args:
        registry: User registry for API key resolution.
        request: The incoming aiohttp request.
        handler: The next handler in the middleware chain.

    Returns:
        A ``401 Unauthorized`` JSON response on auth failure, or the
        response from the downstream handler on success.
    """
    # Note: /healthz, /openapi.json and /docs are registered on the root app,
    # not on the /v1 sub-app where this middleware lives, so they are never
    # passed to this function. No path-based bypass needed here.
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        logger.debug("Auth failed: missing or malformed Authorization header")
        return web.json_response(
            {"error": "Unauthorized", "detail": "Bearer token required"},
            status=401,
        )

    raw_token = auth_header[len("Bearer "):]
    if not raw_token:
        logger.debug("Auth failed: empty token")
        return web.json_response(
            {"error": "Unauthorized", "detail": "Bearer token required"},
            status=401,
        )

    # Resolve using REST channel — UserRegistry hashes the token internally
    # so we pass the raw key as the "raw_id" part of the sender_id.
    sender_id = f"rest:{raw_token}"
    user_record = registry.resolve_user(
        sender_id=sender_id,
        channel="rest",
    )

    if user_record is None:
        # Log only token length to avoid leaking sensitive data
        logger.debug("Auth failed: unknown token (length=%d)", len(raw_token))
        return web.json_response(
            {"error": "Unauthorized", "detail": "Invalid API key"},
            status=401,
        )

    if user_record.blocked:
        logger.debug(
            "Auth failed: user %s is blocked", user_record.user_id
        )
        return web.json_response(
            {"error": "Unauthorized", "detail": "Account suspended"},
            status=401,
        )

    # Store resolved identity on the request for downstream handlers.
    # sender_id uses the stable portail user_id — the raw token is never
    # propagated into the Redis pipeline, logs, or memory store.
    request["user_record"] = user_record
    request["sender_id"] = f"rest:{user_record.user_id}"

    return await handler(request)
