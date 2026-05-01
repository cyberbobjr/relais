"""WhatsApp channel adapter — NativeAiguilleur implementation (Baileys gateway).

Bridges the Baileys HTTP gateway (fazer-ai/baileys-api) and the RELAIS Redis bus:
- Produces:   relais:messages:incoming              (new user messages)
- Consumes:   relais:messages:outgoing:whatsapp      (bot replies + progress events)
- Subscribes: relais:streaming:start:whatsapp        (Pub/Sub — streaming start signal)
- Reads:      relais:messages:streaming:whatsapp:{corr_id}  (per-request token chunks)

Streaming is handled by buffering: the adapter subscribes to
``relais:streaming:start:whatsapp`` (Pub/Sub); for each signal it spawns a
``_consume_streaming_reply`` task that reads token chunks via XREAD until
``is_final=1``, then sends a single assembled WhatsApp message.  Outgoing
envelopes with ``context["atelier"]["streamed"] == True`` are ACKed and dropped
to avoid duplicate delivery.

Uses an aiohttp webhook server to receive events from the gateway, and an
aiohttp client to send messages back via the gateway REST API.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from collections import OrderedDict
from typing import Any

from common.contexts import CTX_AIGUILLEUR, CTX_ATELIER, AiguilleurCtx, AtelierCtx, ensure_ctx
from common.envelope import Envelope
from common.envelope_actions import (
    ACTION_MESSAGE_INCOMING,
    ACTION_MESSAGE_OUTGOING,
    ACTION_MESSAGE_PROGRESS,
)
from common.markdown_converter import convert_md_to_whatsapp
from common.redis_client import RedisClient
from common.streams import (
    KEY_WHATSAPP_PAIRING,
    STREAM_INCOMING,
    STREAM_OUTGOING_FAILED,
    pubsub_streaming_start,
    stream_outgoing,
    stream_streaming,
)
from aiguilleur.core.native import NativeAiguilleur

logger = logging.getLogger("relais.whatsapp")


# ---------------------------------------------------------------------------
# JID ↔ E.164 normalisation
# ---------------------------------------------------------------------------

def normalize_whatsapp_id(jid: str) -> str:
    """Convert a WhatsApp JID to an E.164 phone number.

    Strips the ``@s.whatsapp.net`` domain and optional ``:device`` suffix,
    then prepends ``+``.

    Args:
        jid: Full JID like ``"33699999999:2@s.whatsapp.net"``.

    Returns:
        E.164 string like ``"+33699999999"``.
    """
    return "+" + jid.split("@")[0].split(":")[0]


def e164_to_jid(e164: str) -> str:
    """Convert an E.164 phone number to a WhatsApp JID.

    Args:
        e164: Phone number like ``"+33699999999"``.

    Returns:
        JID like ``"33699999999@s.whatsapp.net"``.
    """
    return e164.lstrip("+") + "@s.whatsapp.net"


# ---------------------------------------------------------------------------
# Message splitting
# ---------------------------------------------------------------------------

def _split_whatsapp_message(content: str, limit: int = 4096) -> list[str]:
    """Split a message into parts within the WhatsApp character limit.

    Tries to split on paragraph breaks, then line breaks, then spaces,
    and finally hard-cuts as a last resort.

    Args:
        content: Full message text.
        limit:   Maximum characters per part.

    Returns:
        List of message parts, each at most ``limit`` characters.
    """
    if len(content) <= limit:
        return [content]

    parts: list[str] = []
    remaining = content
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break

        # Try to find a good split point
        chunk = remaining[:limit]
        split_at = -1

        # Prefer paragraph break
        idx = chunk.rfind("\n\n")
        if idx > 0:
            split_at = idx + 2
        else:
            # Try line break
            idx = chunk.rfind("\n")
            if idx > 0:
                split_at = idx + 1
            else:
                # Try space
                idx = chunk.rfind(" ")
                if idx > 0:
                    split_at = idx + 1
                else:
                    # Hard cut
                    split_at = limit

        parts.append(remaining[:split_at])
        remaining = remaining[split_at:]

    return parts


# ---------------------------------------------------------------------------
# Adapter lifecycle
# ---------------------------------------------------------------------------

class WhatsAppAiguilleur(NativeAiguilleur):
    """WhatsApp channel adapter wrapping a Baileys HTTP gateway.

    Spawns an aiohttp webhook server to receive gateway events and an
    outgoing consumer loop to relay pipeline replies.
    """

    async def run(self) -> None:
        """Main entry point called by NativeAiguilleur in a dedicated thread.

        Reads configuration from environment variables, instantiates the
        business logic client, and runs until ``stop_event`` is set.

        Config errors (missing env vars) cause a clean return — no crash loop.
        Transient errors propagate so NativeAiguilleur restarts with backoff.
        """
        phone_number = os.environ.get("WHATSAPP_PHONE_NUMBER", "")
        if not phone_number:
            logger.error(
                "WHATSAPP_PHONE_NUMBER not set — WhatsApp adapter disabled. "
                "Set it in .env and restart."
            )
            return

        webhook_secret = os.environ.get("WHATSAPP_WEBHOOK_SECRET", "")
        if not webhook_secret:
            logger.error(
                "WHATSAPP_WEBHOOK_SECRET not set — WhatsApp adapter disabled. "
                "Set it in .env (min 6 chars) and restart."
            )
            return

        redis_client = RedisClient("aiguilleur")
        redis_conn = await redis_client.get_connection()
        client = _RelaisWhatsAppClient(adapter=self, redis=redis_conn)
        try:
            await client.ensure_gateway_ready()
            await client.start()
        finally:
            await client.close()
            await redis_client.close()


# ---------------------------------------------------------------------------
# Business logic
# ---------------------------------------------------------------------------

class _RelaisWhatsAppClient:
    """Core WhatsApp adapter logic — webhook server + outgoing consumer.

    Attributes:
        seen_message_ids: Dedup LRU for incoming webhook retries.
        sent_message_ids: Anti-loop LRU for RELAIS's own sent messages.
        consumer_name: Redis consumer group member name.
    """

    def __init__(self, adapter: WhatsAppAiguilleur, redis: Any) -> None:
        self._adapter = adapter
        self._redis = redis
        self._log = logger
        self._stop = adapter.stop_event

        self._gateway_url = os.environ.get("WHATSAPP_GATEWAY_URL", "http://localhost:3025")
        self._phone_number = os.environ.get("WHATSAPP_PHONE_NUMBER", "")
        self._api_key = os.environ.get("WHATSAPP_API_KEY", "")
        self._webhook_secret = os.environ.get("WHATSAPP_WEBHOOK_SECRET", "")
        self._webhook_port = int(os.environ.get("WHATSAPP_WEBHOOK_PORT", "8765"))
        self._webhook_host = os.environ.get("WHATSAPP_WEBHOOK_HOST", "127.0.0.1")

        self._self_jid = e164_to_jid(self._phone_number)

        self.seen_message_ids: OrderedDict[str, None] = OrderedDict()
        self.sent_message_ids: OrderedDict[str, None] = OrderedDict()
        self.consumer_name = f"whatsapp_{os.getpid()}"

        self._http: Any = None  # aiohttp.ClientSession, created in start()
        self._site: Any = None  # aiohttp.web.TCPSite
        # Strong references to in-flight streaming consumer tasks (prevents GC)
        self._streaming_tasks: set[asyncio.Task] = set()

    async def ensure_gateway_ready(self) -> None:
        """Poll the gateway health endpoint until reachable (max 30s).

        Never blocks or raises — logs a warning and returns if unreachable.
        """
        import aiohttp

        for attempt in range(15):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{self._gateway_url}/status",
                        timeout=aiohttp.ClientTimeout(total=2),
                    ) as resp:
                        if resp.status == 200:
                            self._log.info(
                                "Gateway reachable at %s. Connection state "
                                "will be reported via webhook.",
                                self._gateway_url,
                            )
                            return
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(2)

        self._log.warning(
            "Gateway not reachable at %s after 30s — adapter will start "
            "but incoming won't work until gateway is up.",
            self._gateway_url,
        )

    async def start(self) -> None:
        """Run webhook server and outgoing consumer concurrently.

        Blocks until ``stop_event`` is set.
        """
        import aiohttp
        from aiohttp import web

        self._http = aiohttp.ClientSession()

        # Build webhook server
        app = web.Application()
        app.router.add_post("/webhook", self._handle_webhook)
        app.router.add_get("/health", self._handle_health)

        runner = web.AppRunner(app)
        await runner.setup()
        self._site = web.TCPSite(runner, self._webhook_host, self._webhook_port)
        await self._site.start()
        self._log.info(
            "Webhook server listening on %s:%d",
            self._webhook_host, self._webhook_port,
        )

        try:
            await asyncio.gather(
                self._consume_outgoing(),
                self._stop_watcher(),
                self._subscribe_streaming_start(),
            )
        finally:
            await runner.cleanup()

    async def _stop_watcher(self) -> None:
        """Poll threading.Event to detect shutdown."""
        while not self._stop.is_set():
            await asyncio.sleep(0.5)
        # Cancel the gather by raising
        raise asyncio.CancelledError

    async def close(self) -> None:
        """Clean up HTTP client session."""
        if self._http and not self._http.closed:
            await self._http.close()

    # ------------------------------------------------------------------
    # Webhook handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: Any) -> Any:
        """Health check endpoint for /settings whatsapp guard."""
        from aiohttp import web
        return web.json_response({"status": "ok"})

    async def _handle_webhook(self, request: Any) -> Any:
        """Main webhook handler — routes events from baileys-api."""
        from aiohttp import web

        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON")

        if not self._verify_webhook_token(body):
            return web.Response(status=401, text="Invalid webhook token")

        event = body.get("event", "")
        try:
            if event == "messages.upsert":
                await self._handle_messages_upsert(body)
            elif event == "connection.update":
                data = body.get("data", {})
                if "qr" in data or "qrDataUrl" in data:
                    await self._handle_qr_event(body)
                elif data.get("connection") == "open":
                    await self._handle_connected_event(body)
                elif data.get("connection") == "close":
                    await self._handle_close_event(body)
                elif data.get("connection") == "reconnecting":
                    self._log.info("WhatsApp connection reconnecting")
                else:
                    self._log.debug("Unhandled connection.update: %s", data)
            else:
                self._log.debug("Ignoring webhook event: %s", event)
        except Exception:
            self._log.exception("Error processing webhook event %s", event)

        return web.Response(status=200, text="OK")

    def _verify_webhook_token(self, body: dict) -> bool:
        """Verify the webhook token using constant-time comparison.

        Args:
            body: Parsed webhook JSON body.

        Returns:
            True if the token matches, False otherwise.
        """
        import hmac
        token = body.get("webhookVerifyToken", "")
        return hmac.compare_digest(str(token), self._webhook_secret)

    # ------------------------------------------------------------------
    # QR / pairing
    # ------------------------------------------------------------------

    def _parse_pairing(self, raw: str) -> dict | None:
        """Parse and validate pairing context from Redis.

        Args:
            raw: JSON string from Redis.

        Returns:
            Parsed dict with required fields, or None if invalid.
        """
        try:
            pairing = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self._log.warning("Malformed pairing context — ignoring")
            return None
        required = ("sender_id", "channel", "session_id", "correlation_id", "reply_to")
        if not all(pairing.get(k) for k in required):
            self._log.warning("Pairing context missing required fields — ignoring")
            return None
        return pairing

    async def _handle_qr_event(self, payload: dict) -> None:
        """Convert QR to ASCII art and relay to the channel that initiated pairing."""
        pairing_raw = await self._redis.get(KEY_WHATSAPP_PAIRING)
        if not pairing_raw:
            self._log.debug("QR received but no active pairing session — ignoring")
            return

        pairing = self._parse_pairing(pairing_raw)
        if pairing is None:
            return

        qr_raw = payload["data"].get("qr", "")
        if not qr_raw:
            # Try qrDataUrl fallback
            qr_data_url = payload["data"].get("qrDataUrl", "")
            if not qr_data_url:
                self._log.warning("QR event received but no qr data — cannot render")
                return
            # qrDataUrl is a data:image/png;base64,... but we need the raw string
            # for ASCII rendering. Log and skip if only image is available.
            self._log.warning("Only qrDataUrl available, not raw QR string — cannot render ASCII")
            return

        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(qr_raw)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        ascii_qr = buf.getvalue()

        content = (
            "Scan this QR code with WhatsApp:\n"
            "WhatsApp > Settings > Linked Devices > Link a Device\n\n"
            f"```\n{ascii_qr}```"
        )

        env = Envelope(
            content=content,
            sender_id=pairing["sender_id"],
            channel=pairing["channel"],
            session_id=pairing["session_id"],
            correlation_id=pairing["correlation_id"],
            action=ACTION_MESSAGE_OUTGOING,
        )
        ensure_ctx(env, CTX_AIGUILLEUR)["reply_to"] = pairing["reply_to"]

        await self._redis.xadd(
            stream_outgoing(pairing["channel"]),
            {"payload": env.to_json()},
        )

        # Update pairing state
        pairing["state"] = "qr_displayed"
        await self._redis.set(KEY_WHATSAPP_PAIRING, json.dumps(pairing), ex=300)

        self._log.info("ASCII QR code relayed to %s channel for pairing", pairing["channel"])

    async def _handle_connected_event(self, payload: dict) -> None:
        """Notify originator that WhatsApp is now connected."""
        pairing_raw = await self._redis.get(KEY_WHATSAPP_PAIRING)
        if not pairing_raw:
            self._log.info("WhatsApp connected (no active pairing context — likely a reconnect)")
            return

        pairing = self._parse_pairing(pairing_raw)
        if pairing is None:
            return
        env = Envelope(
            content="WhatsApp successfully linked! The adapter is now operational.",
            sender_id=pairing["sender_id"],
            channel=pairing["channel"],
            session_id=pairing["session_id"],
            correlation_id=pairing["correlation_id"],
            action=ACTION_MESSAGE_OUTGOING,
        )
        ensure_ctx(env, CTX_AIGUILLEUR)["reply_to"] = pairing["reply_to"]

        await self._redis.xadd(
            stream_outgoing(pairing["channel"]),
            {"payload": env.to_json()},
        )
        await self._redis.delete(KEY_WHATSAPP_PAIRING)
        self._log.info("WhatsApp pairing confirmed — adapter fully operational")

    async def _handle_close_event(self, payload: dict) -> None:
        """Handle connection close — notify admin if during pairing."""
        error_detail = payload.get("data", {}).get("lastDisconnect", {}).get("error", "unknown")

        pairing_raw = await self._redis.get(KEY_WHATSAPP_PAIRING)
        if not pairing_raw:
            self._log.warning(
                "WhatsApp connection closed (runtime): %s — "
                "baileys-api will auto-reconnect",
                error_detail,
            )
            return

        pairing = self._parse_pairing(pairing_raw)
        if pairing is None:
            return

        if "wrong_phone_number" in str(error_detail).lower():
            msg = (
                "WhatsApp pairing failed: wrong phone number.\n"
                "Check WHATSAPP_PHONE_NUMBER and re-run /settings whatsapp."
            )
        else:
            msg = (
                f"WhatsApp pairing failed: connection closed ({error_detail}).\n"
                "Re-run /settings whatsapp to try again."
            )

        env = Envelope(
            content=msg,
            sender_id=pairing["sender_id"],
            channel=pairing["channel"],
            session_id=pairing["session_id"],
            correlation_id=pairing["correlation_id"],
            action=ACTION_MESSAGE_OUTGOING,
        )
        ensure_ctx(env, CTX_AIGUILLEUR)["reply_to"] = pairing["reply_to"]

        await self._redis.xadd(
            stream_outgoing(pairing["channel"]),
            {"payload": env.to_json()},
        )
        await self._redis.delete(KEY_WHATSAPP_PAIRING)
        self._log.warning("WhatsApp pairing failed: %s", error_detail)

    # ------------------------------------------------------------------
    # Incoming message processing
    # ------------------------------------------------------------------

    async def _handle_messages_upsert(self, payload: dict) -> None:
        """Process incoming messages.upsert webhook event.

        Only processes real-time messages (type=notify), not history sync.
        Individual message failures are logged but do not block other messages.

        Args:
            payload: Full webhook body with ``data.messages`` array.
        """
        if payload["data"].get("type") != "notify":
            return

        for message in payload["data"].get("messages", []):
            try:
                await self._process_single_message(message)
            except Exception:
                self._log.exception(
                    "Failed to process message %s",
                    message.get("key", {}).get("id", "?"),
                )

    async def _process_single_message(self, message: dict) -> None:
        """Process a single incoming WhatsApp message.

        Handles deduplication, routing (self-chat vs external, fromMe logic),
        text extraction, and envelope construction.

        Args:
            message: Single Baileys message dict from messages.upsert.
        """
        msg_id = message.get("key", {}).get("id", "")

        # Deduplication — webhook retry protection (OrderedDict LRU)
        if msg_id in self.seen_message_ids:
            return
        self.seen_message_ids[msg_id] = None
        if len(self.seen_message_ids) > 1000:
            self.seen_message_ids.popitem(last=False)

        jid = message.get("key", {}).get("remoteJid", "")
        from_me = message.get("key", {}).get("fromMe", False)
        is_self_chat = jid == self._self_jid

        # Filter group messages
        if "@g.us" in jid:
            return

        # Routing logic
        if is_self_chat:
            if not from_me:
                return  # impossible in practice, defensive
            # fromMe in self-chat: admin talking to RELAIS
            # Anti-loop: skip if this is a message RELAIS sent
            if msg_id in self.sent_message_ids:
                return
            sender_e164 = normalize_whatsapp_id(self._self_jid)
        else:
            if from_me:
                return  # admin replying manually
            sender_e164 = normalize_whatsapp_id(jid)

        # Extract text content
        text = self._extract_text_content(message)
        if text is None:
            return

        # Build Envelope
        sender_id = f"whatsapp:{sender_e164}"
        reply_jid = jid if not is_self_chat else self._self_jid

        # Read config live from adapter
        config = self._adapter.config

        envelope = Envelope(
            content=text,
            sender_id=sender_id,
            channel="whatsapp",
            session_id=f"whatsapp:{sender_e164}",
            action=ACTION_MESSAGE_INCOMING,
        )
        ctx: AiguilleurCtx = ensure_ctx(envelope, CTX_AIGUILLEUR)  # type: ignore[assignment]
        ctx["channel_profile"] = config.profile
        ctx["channel_prompt_path"] = config.prompt_path
        ctx["content_type"] = "text"
        ctx["reply_to"] = reply_jid

        await self._redis.xadd(STREAM_INCOMING, {"payload": envelope.to_json()})

    @staticmethod
    def _extract_text_content(message: dict) -> str | None:
        """Extract text from various WhatsApp message formats.

        Priority: plain text > extended text > image caption > video caption.

        Args:
            message: Single Baileys message dict.

        Returns:
            Extracted text content, or None for non-text messages.
        """
        msg = message.get("message", {})
        if msg is None:
            return None
        return (
            msg.get("conversation")
            or (msg.get("extendedTextMessage") or {}).get("text")
            or (msg.get("imageMessage") or {}).get("caption")
            or (msg.get("videoMessage") or {}).get("caption")
        )

    # ------------------------------------------------------------------
    # Outgoing consumer
    # ------------------------------------------------------------------

    async def _consume_outgoing(self) -> None:
        """Consume replies from the pipeline and send via baileys-api.

        Creates a consumer group on startup (idempotent) and reads from
        ``relais:messages:outgoing:whatsapp`` with XREADGROUP.
        """
        out_stream = stream_outgoing("whatsapp")
        group = "whatsapp_relay_group"

        # Create consumer group (idempotent)
        try:
            await self._redis.xgroup_create(out_stream, group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

        self._log.info("Outgoing consumer ready: %s / %s", group, self.consumer_name)

        while not self._stop.is_set():
            try:
                messages = await self._redis.xreadgroup(
                    group, self.consumer_name,
                    {out_stream: ">"},
                    count=10, block=1000,
                )
            except Exception:
                self._log.exception("XREADGROUP error")
                await asyncio.sleep(1)
                continue

            if not messages:
                continue

            for _, entries in messages:
                for msg_id, msg_data in entries:
                    try:
                        acked = await self._process_outgoing(out_stream, group, msg_id, msg_data)
                        if acked:
                            await self._redis.xack(out_stream, group, msg_id)
                    except Exception:
                        self._log.exception(
                            "Error processing outgoing %s — leaving in PEL for re-delivery",
                            msg_id,
                        )

    async def _process_outgoing(
        self,
        stream: str,
        group: str,
        msg_id: str,
        msg_data: dict,
    ) -> bool:
        """Process a single outgoing message from the pipeline.

        Args:
            stream: Redis stream name.
            group: Consumer group name.
            msg_id: Redis message ID.
            msg_data: Raw message data from Redis.

        Returns:
            True if the message was successfully sent (or routed to DLQ)
            and should be ACKed. False to leave in PEL for re-delivery.
        """
        payload = msg_data.get("payload") or msg_data.get(b"payload")
        if isinstance(payload, bytes):
            payload = payload.decode()
        envelope = Envelope.from_json(payload)

        # Skip progress events — no typing indicator in MVP
        if envelope.action == ACTION_MESSAGE_PROGRESS:
            return True  # ACK — nothing to deliver

        # LLM reply already delivered via streaming stream — skip to avoid duplicate
        atelier_ctx: AtelierCtx = envelope.context.get(CTX_ATELIER, {})  # type: ignore[assignment]
        if atelier_ctx.get("streamed"):
            self._log.debug(
                "Skipping outgoing:whatsapp for %s — already delivered via streaming",
                envelope.correlation_id[:8],
            )
            return True  # ACK — streaming consumer already sent the reply

        aig_ctx: AiguilleurCtx = envelope.context.get(CTX_AIGUILLEUR, {})  # type: ignore[assignment]
        to_jid = aig_ctx.get("reply_to", "")
        if not to_jid:
            self._log.warning("Outgoing envelope has no reply_to — skipping")
            return True  # ACK — cannot deliver without destination

        # Apply WhatsApp markdown conversion
        content = convert_md_to_whatsapp(envelope.content)

        # Split and send
        parts = _split_whatsapp_message(content)
        for part in parts:
            result = await self._send_message(to_jid, part)
            if result is None:
                # Send failed — route to DLQ, then ACK
                try:
                    await self._redis.xadd(
                        STREAM_OUTGOING_FAILED,
                        {
                            "source": "whatsapp",
                            "message_id": msg_id,
                            "payload": envelope.to_json(),
                            "reason": "send_message_failed",
                        },
                    )
                except Exception:
                    self._log.exception("Failed to route to DLQ")
                    return False  # Leave in PEL — neither sent nor DLQ'd
                return True  # ACK — routed to DLQ

        return True  # ACK — all parts sent successfully

    async def _consume_streaming_reply(self, envelope: Envelope) -> None:
        """Buffer token-by-token chunks from the streaming stream and send once complete.

        Reads from ``relais:messages:streaming:whatsapp:{corr_id}`` via XREAD
        until an entry with ``is_final=1`` is received, then assembles all
        chunks into a single message and delivers it via WhatsApp.

        Args:
            envelope: The original request envelope (for corr_id, reply_to, etc.).
        """
        if self._redis is None:
            self._log.error("Redis connection unavailable for streaming reply consumer")
            return

        corr_id = envelope.correlation_id
        stream_key = stream_streaming("whatsapp", corr_id)
        last_id = "0-0"
        buffer: list[str] = []
        deadline = asyncio.get_running_loop().time() + 300

        aig_ctx: AiguilleurCtx = envelope.context.get(CTX_AIGUILLEUR, {})  # type: ignore[assignment]
        to_jid = aig_ctx.get("reply_to", "")

        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    self._log.warning("Streaming reply timed out for %s", corr_id[:8])
                    return

                results = await self._redis.xread(
                    streams={stream_key: last_id},
                    count=50,
                    block=min(5000, int(remaining * 1000)),
                )
                if not results:
                    continue

                for _, entries in results:
                    for entry_id, fields in entries:
                        last_id = entry_id
                        chunk = fields.get("chunk", "")
                        is_final = fields.get("is_final", "0") == "1"

                        if chunk:
                            buffer.append(chunk)

                        if is_final:
                            full_text = "".join(buffer)
                            if to_jid and full_text.strip():
                                for part in _split_whatsapp_message(full_text):
                                    await self._send_message(to_jid, part)
                            return
        except Exception as exc:
            self._log.error(
                "Streaming reply consumer error for %s: %s", corr_id[:8], exc
            )

    async def _subscribe_streaming_start(self) -> None:
        """Subscribe to relais:streaming:start:whatsapp and spawn reply consumers.

        Listens on the Pub/Sub channel that Atelier publishes to before starting
        agent execution. For each start signal, the original request envelope is
        parsed and a ``_consume_streaming_reply`` task is spawned.
        """
        if self._redis is None:
            self._log.error("Redis connection unavailable for streaming start subscriber")
            return

        pubsub = self._redis.pubsub()
        await pubsub.subscribe(pubsub_streaming_start("whatsapp"))
        self._log.info("Subscribed to %s", pubsub_streaming_start("whatsapp"))

        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    data = message.get("data", b"")
                    if isinstance(data, bytes):
                        data = data.decode()
                    envelope = Envelope.from_json(data)
                except Exception as exc:
                    self._log.error("Failed to parse streaming start envelope: %s", exc)
                    continue

                task = asyncio.get_running_loop().create_task(
                    self._consume_streaming_reply(envelope)
                )
                self._streaming_tasks.add(task)
                task.add_done_callback(self._streaming_tasks.discard)
        except Exception as exc:
            self._log.error("Streaming start subscriber error: %s", exc)

    async def _send_message(self, to_jid: str, text: str) -> str | None:
        """Send a text message via baileys-api.

        Args:
            to_jid: Recipient JID.
            text: Message text.

        Returns:
            Message ID on success, None on error.
        """
        url = f"{self._gateway_url}/connections/{self._phone_number}/send-message"
        headers = {"x-api-key": self._api_key, "Content-Type": "application/json"}
        import aiohttp as _aiohttp

        try:
            async with self._http.post(
                url,
                json={"jid": to_jid, "messageContent": {"text": text}},
                headers=headers,
                timeout=_aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    self._log.warning("baileys-api send error %d: %s", resp.status, body)
                    return None
                data = await resp.json()
                msg_id = data.get("data", {}).get("key", {}).get("id")
                if msg_id:
                    self.sent_message_ids[msg_id] = None
                    if len(self.sent_message_ids) > 1000:
                        self.sent_message_ids.popitem(last=False)
                return msg_id
        except Exception:
            self._log.exception("Failed to send message to %s", to_jid)
            return None
