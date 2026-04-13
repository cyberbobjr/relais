"""REST adapter HTTP server factory.

Creates and configures the aiohttp Application for the REST channel adapter.

Endpoints:
    GET  /healthz            — liveness probe (no auth)
    POST /v1/messages        — send a message and receive the LLM reply

Request flow (classic JSON mode):
    1. BearerAuthMiddleware resolves the caller's identity.
    2. Handler validates body, generates correlation_id, builds Envelope.
    3. Registers Future in ResponseCorrelator.
    4. XADDs Envelope to relais:messages:incoming:rest.
    5. Awaits Future (timeout = config["request_timeout"]).
    6. Returns 200 with {content, correlation_id, session_id[, traces]}.
    7. On timeout → 504; on disconnect → cancel Future in finally.

Request flow (SSE streaming mode):
    Accept: text/event-stream triggers streaming mode. The handler opens a
    StreamResponse and forwards token chunks from
    relais:messages:streaming:rest:{correlation_id} until the final reply
    arrives on relais:messages:outgoing:rest.

Background task (_consume_outgoing):
    Reads relais:messages:outgoing:rest via consumer group rest_relay_group
    and calls correlator.resolve() for each message. Orphan messages (no
    registered Future) are DEBUG-logged and ACKed normally.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from aiohttp import web

from common.contexts import CTX_AIGUILLEUR
from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_INCOMING
from common.streams import (
    STREAM_INCOMING,
    stream_outgoing,
    stream_streaming,
)

from aiguilleur.channels.rest.auth import make_bearer_auth_middleware
from aiguilleur.channels.rest.sse import HEARTBEAT, format_sse

if TYPE_CHECKING:
    from aiguilleur.channels.rest.correlator import ResponseCorrelator
    from portail.user_registry import UserRegistry

logger = logging.getLogger("aiguilleur.rest.server")

_CHANNEL = "rest"
_STREAM_IN = f"{STREAM_INCOMING}:{_CHANNEL}"


# ---------------------------------------------------------------------------
# Standalone handlers (importable for tests and sub-app mounting)
# ---------------------------------------------------------------------------


async def healthz_handler(request: web.Request) -> web.Response:
    """Liveness probe — no authentication required.

    Args:
        request: Incoming HTTP request.

    Returns:
        200 JSON ``{"status": "ok", "channel": "rest"}``.
    """
    return web.json_response({"status": "ok", "channel": _CHANNEL})


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def create_app(
    adapter: Any,
    redis_conn: Any,
    correlator: "ResponseCorrelator",
    registry: "UserRegistry",
    config: dict[str, Any],
) -> web.Application:
    """Build and return a configured aiohttp Application.

    Args:
        adapter: The owning ``RestAiguilleur`` (used for config access).
        redis_conn: Async Redis connection (real or fakeredis).
        correlator: Shared ``ResponseCorrelator`` instance.
        registry: Loaded ``UserRegistry`` for Bearer token resolution.
        config: Flat dict with server settings:
            - ``cors_origins`` (list[str]): Allowed CORS origins.
            - ``request_timeout`` (float): Seconds to wait for LLM reply.
            - ``include_traces`` (bool): Whether to include traces in JSON response.

    Returns:
        A fully-configured ``web.Application`` ready for ``AppRunner``.
    """
    cors_origins: list[str] = config.get("cors_origins", ["*"])
    if "*" in cors_origins:
        logger.warning(
            "REST adapter: cors_origins=['*'] — all origins are allowed. "
            "Restrict cors_origins in aiguilleur.yaml for production."
        )

    auth_middleware = make_bearer_auth_middleware(registry)

    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        response = await handler(request)
        origin = request.headers.get("Origin", "")
        if "*" in cors_origins or origin in cors_origins:
            response.headers["Access-Control-Allow-Origin"] = (
                "*" if "*" in cors_origins else origin
            )
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, Accept"
            )
            response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
        return response

    @web.middleware
    async def options_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            return web.Response(
                status=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept",
                    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                },
            )
        return await handler(request)

    async def post_message(request: web.Request) -> web.Response:
        """Handle POST /v1/messages.

        Parses body, builds Envelope, publishes to Redis, awaits reply.

        Args:
            request: Authenticated incoming request.

        Returns:
            200 with reply, 400 on bad input, 504 on timeout.
        """
        # --- Parse body ---
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "Bad Request", "detail": "Invalid JSON body"},
                status=400,
            )

        content = body.get("content")
        if not content or not str(content).strip():
            return web.json_response(
                {"error": "Bad Request", "detail": "'content' field is required and must be non-empty"},
                status=400,
            )

        session_id: str = str(body.get("session_id") or uuid.uuid4())
        media_refs_raw: list = body.get("media_refs") or []

        correlation_id = str(uuid.uuid4())
        user_record = request["user_record"]
        sender_id: str = request["sender_id"]

        # --- Read channel config for profile ---
        channel_profile: str | None = None
        channel_prompt_path: str | None = None
        try:
            cfg = adapter.config
            channel_profile = cfg.profile_ref.profile
            channel_prompt_path = cfg.prompt_path
        except Exception:
            pass

        # --- Determine accept type ---
        accept = request.headers.get("Accept", "")
        is_sse = "text/event-stream" in accept

        # --- Build Envelope ---
        envelope = Envelope(
            content=str(content),
            sender_id=sender_id,
            channel=_CHANNEL,
            session_id=session_id,
            correlation_id=correlation_id,
            action=ACTION_MESSAGE_INCOMING,
            context={
                CTX_AIGUILLEUR: {
                    "content_type": "text",
                    "reply_to": correlation_id,
                    "correlation_id": correlation_id,
                    "channel_profile": channel_profile,
                    "channel_prompt_path": channel_prompt_path,
                    "streaming": is_sse,
                }
            },
        )

        request_timeout: float = float(config.get("request_timeout", 30))
        include_traces: bool = bool(config.get("include_traces", False))

        if is_sse:
            return await _handle_sse(
                request, envelope, correlation_id, session_id,
                redis_conn, correlator, request_timeout,
            )

        # --- Classic JSON mode ---
        future = await correlator.register(correlation_id)
        try:
            await redis_conn.xadd(_STREAM_IN, {"payload": envelope.to_json()})
            logger.info(
                "Published REST message corr=%s session=%s",
                correlation_id[:8],
                session_id,
            )

            reply_envelope: Envelope = await asyncio.wait_for(future, timeout=request_timeout)

        except asyncio.TimeoutError:
            logger.warning("REST request timed out corr=%s", correlation_id[:8])
            return web.json_response(
                {"error": "Gateway Timeout", "detail": "LLM reply not received in time"},
                status=504,
            )
        except asyncio.CancelledError:
            logger.debug("REST request cancelled corr=%s", correlation_id[:8])
            raise
        finally:
            await correlator.cancel(correlation_id)

        response_body: dict[str, Any] = {
            "content": reply_envelope.content,
            "correlation_id": correlation_id,
            "session_id": session_id,
        }
        if include_traces:
            response_body["traces"] = reply_envelope.traces

        return web.json_response(response_body)

    # --- App assembly ---
    # Auth middleware only applies to routes that need it
    app = web.Application(middlewares=[options_middleware, cors_middleware])
    app["_redis"] = redis_conn
    app["_correlator"] = correlator
    app["_config"] = config

    app.router.add_get("/healthz", healthz_handler)

    # Sub-app with new-style auth middleware for /v1
    api_app = web.Application(middlewares=[auth_middleware])
    api_app.router.add_post("/messages", post_message)
    app.add_subapp("/v1", api_app)

    return app


# ---------------------------------------------------------------------------
# SSE handler (extracted for clarity)
# ---------------------------------------------------------------------------


async def _handle_sse(
    request: web.Request,
    envelope: Envelope,
    correlation_id: str,
    session_id: str,
    redis_conn: Any,
    correlator: "ResponseCorrelator",
    request_timeout: float,
) -> web.StreamResponse:
    """Handle SSE streaming mode for POST /v1/messages.

    Publishes the envelope, then streams token chunks from
    ``relais:messages:streaming:rest:{correlation_id}`` until the final
    outgoing message is detected.

    Args:
        request: Authenticated incoming request.
        envelope: Pre-built Envelope with streaming=True.
        correlation_id: Correlation ID for this request.
        session_id: Session ID for this request.
        redis_conn: Async Redis connection.
        correlator: Response correlator.
        request_timeout: Max seconds to wait for the full response.

    Returns:
        An open ``web.StreamResponse`` with SSE frames.
    """
    response = web.StreamResponse()
    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    await response.prepare(request)

    future = await correlator.register(correlation_id)
    streaming_stream = stream_streaming(_CHANNEL, correlation_id)

    try:
        await redis_conn.xadd(_STREAM_IN, {"payload": envelope.to_json()})

        # Stream tokens
        last_id = "0"
        deadline = asyncio.get_event_loop().time() + request_timeout

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break

            # Check if future is done (final message arrived)
            if future.done():
                break

            try:
                results = await asyncio.wait_for(
                    redis_conn.xread({streaming_stream: last_id}, count=10, block=500),
                    timeout=min(1.0, remaining),
                )
            except (asyncio.TimeoutError, Exception):
                # Send heartbeat to keep connection alive
                await response.write(HEARTBEAT)
                continue

            if not results:
                await response.write(HEARTBEAT)
                continue

            for _, messages in results:
                for msg_id, data in messages:
                    last_id = msg_id
                    token = data.get(b"token") or data.get("token") or b""
                    if isinstance(token, bytes):
                        token = token.decode()
                    if token:
                        frame = format_sse("token", json.dumps({"t": token}))
                        await response.write(frame)

        # Send final done event
        if future.done() and not future.cancelled():
            final = future.result()
            frame = format_sse("done", json.dumps({
                "content": final.content,
                "correlation_id": correlation_id,
                "session_id": session_id,
            }))
            await response.write(frame)

    except ConnectionResetError:
        logger.debug("SSE client disconnected corr=%s", correlation_id[:8])
    except asyncio.CancelledError:
        logger.debug("SSE handler cancelled corr=%s", correlation_id[:8])
    finally:
        await correlator.cancel(correlation_id)
        # Best-effort cleanup of streaming stream
        try:
            await redis_conn.delete(streaming_stream)
        except Exception:
            pass

    await response.write_eof()
    return response
