"""REST adapter HTTP server factory.

Creates and configures the aiohttp Application for the REST channel adapter.

Endpoints:
    GET  /healthz            — liveness probe (no auth)
    GET  /openapi.json       — OpenAPI 3.0 spec (no auth)
    GET  /docs               — Swagger UI (no auth)
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
import re
import uuid
from typing import TYPE_CHECKING, Any, Callable, Awaitable

from aiohttp import web

from common.contexts import CTX_AIGUILLEUR
from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_INCOMING
from common.streams import (
    STREAM_INCOMING,
    stream_streaming,
)

from aiguilleur.channels.rest.auth import make_bearer_auth_middleware
from aiguilleur.channels.rest.sse import HEARTBEAT, format_sse

if TYPE_CHECKING:
    from aiguilleur.channels.rest.correlator import ResponseCorrelator
    from portail.user_registry import UserRegistry

logger = logging.getLogger("aiguilleur.rest.server")

_CHANNEL = "rest"
_STREAM_IN = STREAM_INCOMING

# Maximum allowed size for the 'content' field (bytes, UTF-8 encoded)
_MAX_CONTENT_BYTES = 32_768  # 32 KB

# Allowed pattern for caller-supplied session_id values
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


# ---------------------------------------------------------------------------
# OpenAPI 3.0 spec
# ---------------------------------------------------------------------------

_OPENAPI_SPEC: dict = {
    "openapi": "3.0.3",
    "info": {
        "title": "RELAIS REST Channel",
        "version": "1.0.0",
        "description": (
            "HTTP/JSON gateway to the RELAIS AI assistant pipeline. "
            "Supports classic JSON responses and Server-Sent Events (SSE) streaming."
        ),
    },
    "servers": [{"url": "/v1", "description": "RELAIS REST API v1"}],
    "components": {
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "API key issued in portail.yaml (api_keys section).",
            }
        },
        "schemas": {
            "MessageRequest": {
                "type": "object",
                "required": ["content"],
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Text of the message to send to the AI assistant.",
                        "example": "What is the weather today?",
                    },
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Conversation session identifier. If omitted a new UUID4 is "
                            "generated automatically."
                        ),
                        "example": "550e8400-e29b-41d4-a716-446655440000",
                    },
                    "media_refs": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Optional list of media references attached to the message.",
                        "default": [],
                    },
                },
            },
            "MessageResponse": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Full text reply from the AI assistant.",
                    },
                    "correlation_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Unique identifier for this request/response pair.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session identifier (echoed from request or newly generated).",
                    },
                    "traces": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Pipeline step traces (only present when include_traces=true in config).",
                    },
                },
            },
            "ErrorResponse": {
                "type": "object",
                "properties": {
                    "error": {"type": "string"},
                    "detail": {"type": "string"},
                },
            },
        },
    },
    "security": [{"bearerAuth": []}],
    "paths": {
        "/messages": {
            "post": {
                "summary": "Send a message and receive the AI reply",
                "description": (
                    "Publishes the message to the RELAIS pipeline and waits for the LLM reply.\n\n"
                    "**Classic JSON mode** (default): waits for the full reply and returns it as JSON.\n\n"
                    "**SSE streaming mode**: set `Accept: text/event-stream` to receive token-by-token "
                    "chunks as Server-Sent Events. Events emitted:\n"
                    "- `token` — `{\"t\": \"<chunk>\"}`\n"
                    "- `done` — `{\"content\": \"<full_reply>\", \"correlation_id\": \"...\", \"session_id\": \"...\"}`\n"
                    "- `: keepalive` (comment line) — heartbeat to keep the connection alive"
                ),
                "operationId": "postMessage",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/MessageRequest"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "AI reply (JSON mode)",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/MessageResponse"}
                            },
                            "text/event-stream": {
                                "schema": {
                                    "type": "string",
                                    "description": "SSE stream of token, progress, done, and error events.",
                                }
                            },
                        },
                    },
                    "400": {
                        "description": "Bad request (missing or invalid body)",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                            }
                        },
                    },
                    "401": {
                        "description": "Unauthorized (missing, invalid, or blocked API key)",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                            }
                        },
                    },
                    "504": {
                        "description": "Gateway timeout (LLM did not reply in time)",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                            }
                        },
                    },
                },
            }
        }
    },
}

_SWAGGER_UI_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>RELAIS REST API — Swagger UI</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({
      url: "/openapi.json",
      dom_id: "#swagger-ui",
      presets: [SwaggerUIBundle.presets.apis],
      deepLinking: true,
    });
  </script>
</body>
</html>
"""


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


async def openapi_handler(request: web.Request) -> web.Response:
    """Serve the OpenAPI 3.0 specification as JSON.

    No authentication required so that API clients and CI tools can
    fetch the spec without a valid API key.

    Args:
        request: Incoming HTTP request.

    Returns:
        200 JSON response containing the OpenAPI 3.0 spec dict.
    """
    return web.json_response(_OPENAPI_SPEC)


async def docs_handler(request: web.Request) -> web.Response:
    """Serve the Swagger UI HTML page.

    Loads Swagger UI from unpkg CDN and points it at ``/openapi.json``.
    No authentication required.

    Args:
        request: Incoming HTTP request.

    Returns:
        200 HTML response with the Swagger UI page.
    """
    return web.Response(text=_SWAGGER_UI_HTML, content_type="text/html")


_SSE_PLAYGROUND_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>RELAIS REST API — SSE Playground</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
           background: #1a1a2e; color: #e0e0e0; padding: 24px; }
    h1 { font-size: 1.3rem; margin-bottom: 16px; color: #8be9fd; }
    .row { display: flex; gap: 12px; margin-bottom: 12px; align-items: end; }
    label { display: block; font-size: 0.8rem; color: #888; margin-bottom: 4px; }
    input, textarea { background: #16213e; border: 1px solid #333; color: #e0e0e0;
                      border-radius: 6px; padding: 8px 10px; font-family: inherit; font-size: 0.9rem; }
    input { width: 100%; }
    textarea { width: 100%; min-height: 60px; resize: vertical; }
    .col { flex: 1; }
    .col-sm { flex: 0 0 160px; }
    button { background: #0f3460; color: #e0e0e0; border: 1px solid #444; border-radius: 6px;
             padding: 8px 20px; cursor: pointer; font-size: 0.9rem; font-family: inherit; }
    button:hover { background: #1a5276; }
    button:disabled { opacity: 0.4; cursor: not-allowed; }
    button.stop { background: #6b2020; }
    button.stop:hover { background: #8b3030; }
    #output { background: #0a0a1a; border: 1px solid #333; border-radius: 8px;
              padding: 16px; min-height: 200px; max-height: 60vh; overflow-y: auto;
              white-space: pre-wrap; word-wrap: break-word; line-height: 1.6; font-size: 0.95rem; }
    .token { color: #f8f8f2; }
    .meta { color: #6272a4; font-size: 0.8rem; }
    .error { color: #ff5555; }
    .done { color: #50fa7b; }
    .info { color: #8be9fd; font-size: 0.85rem; }
    #status { font-size: 0.8rem; color: #888; margin-bottom: 8px; }
  </style>
</head>
<body>
  <h1>SSE Playground</h1>

  <div class="row">
    <div class="col">
      <label>API Key</label>
      <input type="password" id="apikey" placeholder="Bearer token" />
    </div>
    <div class="col-sm">
      <label>Session ID (optional)</label>
      <input type="text" id="session" placeholder="auto-generated" />
    </div>
  </div>

  <div class="row">
    <div class="col">
      <label>Message</label>
      <textarea id="content" placeholder="Type your message here..."></textarea>
    </div>
  </div>

  <div class="row">
    <button id="btn-send" onclick="sendSSE()">Send (SSE)</button>
    <button id="btn-stop" class="stop" onclick="stopSSE()" disabled>Stop</button>
    <button onclick="clearOutput()">Clear</button>
  </div>

  <div id="status"></div>
  <div id="output"></div>

  <script>
    let controller = null;

    function el(id) { return document.getElementById(id); }

    function append(html) {
      const o = el("output");
      o.innerHTML += html;
      o.scrollTop = o.scrollHeight;
    }

    function setStatus(text) { el("status").textContent = text; }
    function clearOutput() { el("output").innerHTML = ""; setStatus(""); }

    function stopSSE() {
      if (controller) { controller.abort(); controller = null; }
      el("btn-send").disabled = false;
      el("btn-stop").disabled = true;
      setStatus("Stopped.");
    }

    let tokenCount = 0;

    async function sendSSE() {
      const apikey = el("apikey").value.trim();
      const content = el("content").value.trim();
      if (!apikey || !content) { alert("API key and message are required."); return; }

      el("btn-send").disabled = true;
      el("btn-stop").disabled = false;
      tokenCount = 0;
      append('<span class="info">--- New request ---</span>\\n');
      setStatus("Connecting...");

      controller = new AbortController();
      const body = { content };
      const session = el("session").value.trim();
      if (session) body.session_id = session;

      try {
        const resp = await fetch("/v1/messages", {
          method: "POST",
          headers: {
            "Authorization": "Bearer " + apikey,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
          },
          body: JSON.stringify(body),
          signal: controller.signal,
        });

        if (!resp.ok) {
          const err = await resp.text();
          append('<span class="error">HTTP ' + resp.status + ': ' + err + '</span>\\n');
          stopSSE();
          return;
        }

        // Check if server returned JSON instead of SSE (non-streaming fallback)
        const ct = resp.headers.get("Content-Type") || "";
        if (ct.includes("application/json")) {
          const data = await resp.json();
          append('<span class="token">' + escapeHtml(data.content || "") + '</span>\\n');
          append('<span class="done">--- Done (non-streaming) ---</span>\\n');
          if (data.session_id) { el("session").value = data.session_id; }
          stopSSE();
          return;
        }

        setStatus("Streaming...");
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\\n");
          buffer = lines.pop();

          let eventType = "";
          for (const line of lines) {
            if (line.startsWith("event: ")) {
              eventType = line.slice(7).trim();
            } else if (line.startsWith("data: ")) {
              const raw = line.slice(6);
              try {
                const data = JSON.parse(raw);
                if (eventType === "token" && data.t) {
                  tokenCount++;
                  append('<span class="token">' + escapeHtml(data.t) + '</span>');
                  setStatus("Streaming... " + tokenCount + " tokens");
                } else if (eventType === "done") {
                  // If no tokens were streamed, display the full content
                  if (tokenCount === 0 && data.content) {
                    append('<span class="token">' + escapeHtml(data.content) + '</span>\\n');
                  }
                  append('\\n<span class="done">--- Done (' + tokenCount + ' tokens) ---</span>\\n');
                  if (data.session_id) {
                    el("session").value = data.session_id;
                    append('<span class="meta">session=' + data.session_id + '</span>\\n');
                  }
                  if (data.correlation_id) {
                    append('<span class="meta">corr=' + data.correlation_id + '</span>\\n');
                  }
                } else if (eventType === "progress") {
                  setStatus(data.event + ": " + (data.detail || ""));
                } else if (eventType === "error") {
                  append('\\n<span class="error">Error: ' + escapeHtml(data.error || raw) + '</span>\\n');
                } else if (line.trim()) {
                  append('<span class="meta">[' + eventType + '] ' + escapeHtml(raw) + '</span>\\n');
                }
              } catch (e) {
                if (raw.trim()) {
                  append('<span class="meta">' + escapeHtml(raw) + '</span>\\n');
                }
              }
              eventType = "";
            }
          }
        }
      } catch (e) {
        if (e.name !== "AbortError") {
          append('<span class="error">' + escapeHtml(e.message) + '</span>\\n');
        }
      }

      stopSSE();
    }

    function escapeHtml(s) {
      return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
    }

    // Ctrl+Enter to send
    el("content").addEventListener("keydown", function(e) {
      if (e.ctrlKey && e.key === "Enter") { e.preventDefault(); sendSSE(); }
    });
  </script>
</body>
</html>
"""


async def sse_playground_handler(request: web.Request) -> web.Response:
    """Serve the SSE playground HTML page.

    Interactive client for testing Server-Sent Events streaming.
    No authentication required (the page sends the Bearer token itself).

    Args:
        request: Incoming HTTP request.

    Returns:
        200 HTML response with the SSE playground.
    """
    return web.Response(text=_SSE_PLAYGROUND_HTML, content_type="text/html")


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

    def _resolve_cors_origin(origin: str) -> str:
        """Return the allowed origin header value for a given request origin."""
        if "*" in cors_origins:
            return "*"
        if origin in cors_origins:
            return origin
        return ""

    @web.middleware
    async def cors_middleware(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.Response]],
    ) -> web.Response:
        response = await handler(request)
        origin = request.headers.get("Origin", "")
        allow_origin = _resolve_cors_origin(origin)
        if allow_origin:
            response.headers["Access-Control-Allow-Origin"] = allow_origin
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, Accept"
            )
            response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
        return response

    @web.middleware
    async def options_middleware(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.Response]],
    ) -> web.Response:
        # Handle OPTIONS preflight — apply the same origin policy as cors_middleware
        # so that cors_origins whitelist is respected for preflight requests too.
        if request.method == "OPTIONS":
            origin = request.headers.get("Origin", "")
            allow_origin = _resolve_cors_origin(origin)
            headers = {
                "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept",
                "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
            }
            if allow_origin:
                headers["Access-Control-Allow-Origin"] = allow_origin
            return web.Response(status=204, headers=headers)
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
        content_str = str(content)
        if len(content_str.encode()) > _MAX_CONTENT_BYTES:
            return web.json_response(
                {"error": "Bad Request", "detail": f"'content' exceeds maximum size of {_MAX_CONTENT_BYTES} bytes"},
                status=413,
            )

        raw_session = body.get("session_id")
        if raw_session is not None:
            raw_session_str = str(raw_session)
            if not _SESSION_ID_RE.match(raw_session_str):
                return web.json_response(
                    {"error": "Bad Request", "detail": "session_id must be 1-64 alphanumeric characters (a-z, A-Z, 0-9, _, -)"},
                    status=400,
                )
            session_id: str = raw_session_str
        else:
            session_id = str(uuid.uuid4())
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
        except Exception as exc:
            logger.warning(
                "Failed to read channel profile from adapter config — using default: %s", exc
            )

        # --- Determine accept type ---
        accept = request.headers.get("Accept", "")
        is_sse = "text/event-stream" in accept

        # --- Build Envelope ---
        envelope = Envelope(
            content=content_str,
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
    app.router.add_get("/openapi.json", openapi_handler)
    app.router.add_get("/docs", docs_handler)
    app.router.add_get("/docs/sse", sse_playground_handler)

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

        # Stream tokens and progress events.
        # Any activity on the streaming stream resets the deadline so that
        # long-running tool-call sequences don't timeout as long as
        # Atelier is publishing progress events.
        last_id = "0"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request_timeout

        while True:
            remaining = deadline - loop.time()
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
                await response.write(HEARTBEAT)
                continue

            if not results:
                await response.write(HEARTBEAT)
                continue

            for _, messages in results:
                for msg_id, data in messages:
                    last_id = msg_id
                    # Any activity = extend deadline
                    deadline = loop.time() + request_timeout

                    entry_type = data.get(b"type") or data.get("type") or b""
                    if isinstance(entry_type, bytes):
                        entry_type = entry_type.decode()

                    if entry_type == "token":
                        chunk = data.get(b"chunk") or data.get("chunk") or b""
                        if isinstance(chunk, bytes):
                            chunk = chunk.decode()
                        if chunk:
                            frame = format_sse("token", json.dumps({"t": chunk}))
                            await response.write(frame)
                    elif entry_type == "progress":
                        event = data.get(b"event") or data.get("event") or b""
                        detail = data.get(b"detail") or data.get("detail") or b""
                        if isinstance(event, bytes):
                            event = event.decode()
                        if isinstance(detail, bytes):
                            detail = detail.decode()
                        frame = format_sse("progress", json.dumps({"event": event, "detail": detail}))
                        await response.write(frame)

        # Send final event
        if future.done() and not future.cancelled():
            final = future.result()
            frame = format_sse("done", json.dumps({
                "content": final.content,
                "correlation_id": correlation_id,
                "session_id": session_id,
            }))
            await response.write(frame)
        else:
            # Timeout or cancelled — send an error event so the client
            # knows the stream ended abnormally (not just an EOF).
            reason = "Request timed out" if not future.done() else "Request cancelled"
            logger.warning("SSE error event corr=%s: %s", correlation_id[:8], reason)
            frame = format_sse("error", json.dumps({
                "error": reason,
                "correlation_id": correlation_id,
            }))
            await response.write(frame)

    except ConnectionResetError:
        logger.debug("SSE client disconnected corr=%s", correlation_id[:8])
    except asyncio.CancelledError:
        logger.debug("SSE handler cancelled corr=%s", correlation_id[:8])
    except Exception as exc:
        # Unexpected error — try to send an error event before closing
        try:
            frame = format_sse("error", json.dumps({
                "error": str(exc),
                "correlation_id": correlation_id,
            }))
            await response.write(frame)
        except Exception:
            pass
        logger.error("SSE handler error corr=%s: %s", correlation_id[:8], exc)
    finally:
        await correlator.cancel(correlation_id)
        # Best-effort cleanup of streaming stream
        try:
            await redis_conn.delete(streaming_stream)
        except Exception as exc:
            logger.warning("Failed to delete streaming stream %s: %s", streaming_stream, exc)

    await response.write_eof()
    return response
