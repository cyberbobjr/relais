"""Discord channel adapter — NativeAiguilleur implementation.

Bridges the Discord API and the RELAIS Redis bus:
- Produces:   relais:messages:incoming             (new user messages)
- Consumes:   relais:messages:outgoing:discord      (bot replies + progress events)
- Subscribes: relais:streaming:start:discord        (Pub/Sub — streaming start signal)
- Reads:      relais:messages:streaming:discord:{corr_id}  (per-request token chunks)

Streaming is handled by buffering: the adapter subscribes to
``relais:streaming:start:discord`` (Pub/Sub); for each signal it spawns a
``_consume_streaming_reply`` task that reads token chunks via XREAD until
``is_final=1``, then sends a single assembled Discord message.  Outgoing
envelopes with ``context["atelier"]["streamed"] == True`` are silently dropped
to avoid duplicate delivery.

Progress events (``action == ACTION_MESSAGE_PROGRESS``) are consumed from
``relais:messages:outgoing:discord`` and sent as inline notifications while
Atelier is still running.  The typing indicator is NOT cancelled on progress
events — it continues until the final reply arrives.  Progress event publishing
is controlled by ``DisplayConfig`` (config/atelier.yaml, section ``display:``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import cast

import certifi

# Fix for macOS SSL certificate verify failed
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

import discord

from common.redis_client import RedisClient
from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_INCOMING, ACTION_MESSAGE_PROGRESS
from common.contexts import CTX_AIGUILLEUR, CTX_ATELIER, AiguilleurCtx, AtelierCtx
from common.config_loader import get_default_llm_profile
from common.streams import STREAM_OUTGOING_FAILED, pubsub_streaming_start, stream_streaming
from aiguilleur.channel_config import ChannelConfig
from aiguilleur.core.native import NativeAiguilleur

logger = logging.getLogger("aiguilleur.discord")

_TYPING_MAX_SECONDS: float = 120.0


def _split_discord_message(content: str, limit: int = 2000) -> list[str]:
    """Split a message into parts that each fit within the Discord character limit.

    Splits preferentially on paragraph breaks (``\\n\\n``), then line breaks
    (``\\n``), then word boundaries (space), then hard-cuts as a last resort.

    Args:
        content: The full message content to split.
        limit: Maximum characters per part. Defaults to 2000 (Discord limit).

    Returns:
        List of message parts, each at most ``limit`` characters long.
    """
    if len(content) <= limit:
        return [content]

    parts: list[str] = []
    while len(content) > limit:
        end = limit
        rest = limit
        for sep in ("\n\n", "\n", " "):
            idx = content.rfind(sep, 0, limit)
            if idx > 0:
                if sep == " ":
                    end = idx       # exclude trailing space from first part
                    rest = idx + 1  # skip the space for the next part
                else:
                    end = idx + len(sep)
                    rest = idx + len(sep)
                break
        parts.append(content[:end])
        content = content[rest:]

    if content:
        parts.append(content)
    return parts


class DiscordAiguilleur(NativeAiguilleur):
    """Discord channel adapter.

    Wraps ``_RelaisDiscordClient`` in a NativeAiguilleur lifecycle.
    The Discord client runs inside the adapter thread's event loop.
    """

    def __init__(self, config: ChannelConfig) -> None:
        super().__init__(config)

    async def run(self) -> None:
        """Start the Discord client and block until stop_event is set."""
        token = os.environ.get("DISCORD_BOT_TOKEN")
        if not token or token == "dummy":
            logger.error(
                "DISCORD_BOT_TOKEN is not set or is 'dummy' — Discord adapter will not start."
            )
            return

        client = _RelaisDiscordClient(stop_event=cast(asyncio.Event, self.stop_event), adapter=self)
        try:
            await client.start(token)
        except asyncio.CancelledError:
            pass
        finally:
            if not client.is_closed():
                await client.close()


# ---------------------------------------------------------------------------
# Internal Discord client
# ---------------------------------------------------------------------------


class _RelaisDiscordClient(discord.Client):
    """Internal Discord client — not exposed outside this module.

    Manages two concerns:
    - Receiving Discord messages and publishing them to ``relais:messages:incoming``.
    - Consuming ``relais:messages:outgoing:discord`` and sending the final reply.

    Tokens are buffered internally: the adapter reads the streaming stream
    and sends a single Discord message once Atelier finishes.
    """

    def __init__(
        self,
        stop_event: asyncio.Event | None = None,
        channel_config: ChannelConfig | None = None,
        adapter: "DiscordAiguilleur | None" = None,
    ) -> None:
        """Initialise the Discord client.

        Args:
            stop_event: Optional event to signal the adapter should stop.
            channel_config: Optional channel configuration snapshot. Only used
                when ``adapter`` is ``None``; prefer passing ``adapter`` so
                that hot-reloaded config (``prompt_path``, …) is always read
                from the live ``adapter.config``.
            adapter: The owning ``DiscordAiguilleur`` instance. When provided,
                ``_get_channel_config()`` delegates to ``adapter.config`` so
                that soft-field changes (``prompt_path``) take effect
                immediately without restarting the Discord client.
        """
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        self._redis_client = RedisClient("aiguilleur")
        self.stream_in = "relais:messages:incoming"
        self.stream_out = "relais:messages:outgoing:discord"
        self.group_name = "discord_relay_group"
        self.consumer_name = f"discord_{os.getpid()}"
        self._redis_conn = None
        # threading.Event or asyncio.Event — we only call is_set()
        self._stop_event = stop_event
        # _adapter is the live source of truth; _channel_config is a fallback.
        self._adapter = adapter
        self._channel_config = channel_config
        # Active typing indicator tasks keyed by correlation_id
        self._typing_tasks: dict[str, asyncio.Task] = {}
        # Strong references to in-flight streaming consumer tasks (prevents GC)
        self._streaming_tasks: set[asyncio.Task] = set()

    def _get_channel_config(self) -> ChannelConfig | None:
        """Return the current channel config, always using the live adapter.config.

        When ``_adapter`` is set (normal runtime path), reads ``adapter.config``
        so that hot-reloaded soft fields (``prompt_path``) are immediately
        visible to the next ``on_message`` call without restarting the Discord
        client.

        Falls back to the ``_channel_config`` snapshot when ``_adapter`` is
        ``None`` (e.g. unit tests that construct the client directly).

        Returns:
            The current ``ChannelConfig``, or ``None`` if neither source is set.
        """
        if self._adapter is not None:
            return self._adapter.config
        return self._channel_config

    async def _typing_loop(
        self, channel: discord.abc.Messageable, correlation_id: str
    ) -> None:
        """Maintain a typing indicator until cancelled or the timeout expires.

        Uses ``channel.typing()`` — discord.py's built-in context manager —
        which sends ``trigger_typing`` every 5 seconds automatically. The task
        sleeps inside the context for up to ``_TYPING_MAX_SECONDS`` as a safety
        net against pipeline failures that would never deliver a reply.
        Cancelling the task (via ``_cancel_typing``) raises ``CancelledError``
        in ``asyncio.sleep``, which exits the context manager cleanly.

        Args:
            channel: The Discord channel or DM to show the indicator in.
            correlation_id: Key used to register this task in ``_typing_tasks``.
        """
        try:
            async with channel.typing():
                await asyncio.sleep(_TYPING_MAX_SECONDS)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("typing indicator error (ignored): %s", exc)
        finally:
            self._typing_tasks.pop(correlation_id, None)

    def _cancel_typing(self, correlation_id: str) -> None:
        """Cancel the typing indicator task for the given correlation ID.

        Safe to call even if no task is registered for that ID.

        Args:
            correlation_id: The correlation ID whose typing task to cancel.
        """
        task = self._typing_tasks.pop(correlation_id, None)
        if task is not None:
            task.cancel()

    async def close(self) -> None:
        """Shut down the client and cancel any pending typing indicator tasks.

        Cancels all active typing tasks before delegating to the parent
        ``discord.Client.close()`` to avoid "task was destroyed but pending"
        warnings on shutdown.
        """
        for task in list(self._typing_tasks.values()):
            task.cancel()
        self._typing_tasks.clear()
        await super().close()

    async def setup_hook(self) -> None:
        """Initialise the Redis connection and launch background tasks.

        Called by discord.py once the client is ready to connect.
        Creates the Redis connection, logs the startup event, and launches
        the outgoing-stream consumer task and the streaming start subscriber.
        """
        self._redis_conn = await self._redis_client.get_connection()
        await self._redis_conn.xadd(
            "relais:logs",
            {
                "level": "INFO",
                "brick": "aiguilleur-discord",
                "message": "Starting Discord API connection",
            },
        )
        self.loop.create_task(self._consume_outgoing_stream())
        _sub_task = self.loop.create_task(self._subscribe_streaming_start())
        self._streaming_tasks.add(_sub_task)
        _sub_task.add_done_callback(self._streaming_tasks.discard)

    async def on_ready(self) -> None:
        """Log successful Discord login."""
        if self.user is not None:
            logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message) -> None:
        """Handle incoming Discord messages and publish them to the Redis bus.

        Only processes messages that mention the bot or are sent in a DM.
        Publishes an ``Envelope`` to ``relais:messages:incoming``.

        Args:
            message: The incoming Discord message event.
        """
        if self.user is None or message.author.id == self.user.id:
            return

        bot_mentioned = self.user in message.mentions
        is_dm = isinstance(message.channel, discord.DMChannel)

        if not (bot_mentioned or is_dm):
            return

        content = message.content.replace(f"<@{self.user.id}>", "").strip()
        if not content:
            content = "Coucou!"

        preview = content[:80] + "…" if len(content) > 80 else content
        logger.debug(
            "RECV discord | author=%s | channel=%s | content=%r",
            message.author.name,
            message.channel,
            preview,
        )

        # Read config dynamically so hot-reloaded soft fields (profile,
        # prompt_path) take effect immediately without restarting.
        cfg = self._get_channel_config()
        if cfg is not None:
            current_profile: str | None = cfg.profile_ref.profile
            if current_profile is None:
                current_profile = get_default_llm_profile()
            current_prompt_path: str | None = cfg.prompt_path
        else:
            current_profile = get_default_llm_profile()
            current_prompt_path = None

        envelope = Envelope(
            channel="discord",
            sender_id=f"discord:{message.author.id}",
            content=content,
            session_id=str(message.channel.id),
            action=ACTION_MESSAGE_INCOMING,
            context={
                CTX_AIGUILLEUR: {
                    "content_type": "text",
                    "reply_to": str(message.channel.id),
                    "access_context": "dm" if is_dm else "server",
                    "channel_profile": current_profile,
                    "channel_prompt_path": current_prompt_path,
                }
            },
        )

        typing_task = self.loop.create_task(
            self._typing_loop(message.channel, envelope.correlation_id)
        )
        self._typing_tasks[envelope.correlation_id] = typing_task

        if self._redis_conn is None:
            logger.error("Redis connection not available for incoming message")
            self._cancel_typing(envelope.correlation_id)
            return

        try:
            await self._redis_conn.xadd(self.stream_in, {"payload": envelope.to_json()})
            logger.info("Queued message from %s", message.author.name)
        except Exception as exc:
            logger.error("Failed to queue message: %s", exc)
            self._cancel_typing(envelope.correlation_id)

    # ------------------------------------------------------------------
    # Outgoing stream helpers
    # ------------------------------------------------------------------

    async def _ensure_consumer_group(self, stream: str, group: str) -> None:
        """Create a Redis consumer group idempotently.

        Silently ignores the ``BUSYGROUP`` error raised when the group already
        exists. Other errors are logged as warnings.

        Args:
            stream: Redis stream key (e.g. ``relais:messages:outgoing:discord``).
            group: Consumer group name to create.
        """
        if self._redis_conn is None:
            logger.warning("Redis connection not available for consumer group creation")
            return
        try:
            await self._redis_conn.xgroup_create(stream, group, id="$", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _resolve_discord_channel(
        self, envelope: Envelope
    ) -> discord.abc.Messageable | None:
        """Resolve the Discord channel or DM to send a reply to.

        Tries ``get_channel()`` first (in-process cache), then falls back to
        fetching the user and opening a DM. This fallback is needed when the
        target is a DM channel that isn't cached (e.g. after a bot restart).

        Args:
            envelope: The outgoing message envelope. Must contain ``reply_to``
                (channel ID) in ``context[CTX_AIGUILLEUR]`` and a ``sender_id``
                of the form ``discord:{user_id}``.

        Returns:
            A Discord messageable (``TextChannel``, ``DMChannel``) or ``None``
            if resolution fails.
        """
        try:
            aiguilleur_ctx: AiguilleurCtx = envelope.context.get(CTX_AIGUILLEUR, {}) # type: ignore[assignment]
            channel_id = int(aiguilleur_ctx.get("reply_to", 0))
            channel = self.get_channel(channel_id)
            if channel is not None:
                return channel # type: ignore
            user_id = int(envelope.sender_id.split(":")[1])
            user = await self.fetch_user(user_id)
            return await user.create_dm()
        except Exception as exc:
            logger.error(
                "Cannot resolve Discord channel for envelope %s: %s",
                envelope.correlation_id,
                exc,
            )
            return None

    async def _deliver_progress_event(
        self,
        envelope: Envelope,
        channel: discord.abc.Messageable,
    ) -> None:
        """Display a progress event notification in Discord.

        Only ``tool_call`` events are shown as ``[outil en cours : {detail}]``.
        ``tool_result`` and ``subagent_start`` events are silently ignored
        (too verbose or too implementation-specific for end users).

        Args:
            envelope: Progress envelope; ``context[CTX_ATELIER]["progress_event"]``
                and ``context[CTX_ATELIER]["progress_detail"]`` carry the event data.
            channel: Discord channel or DM to send the notification to.
        """
        atelier_ctx: AtelierCtx = envelope.context.get(CTX_ATELIER, {}) # type: ignore[assignment]
        event = atelier_ctx.get("progress_event", "")
        if not event:
            return
        detail = atelier_ctx.get("progress_detail", "")
        try:
            await channel.send(f"{event} : [{detail}]")
        except Exception as exc:
            logger.debug("Progress event delivery failed (ignored): %s", exc)

    async def _deliver_outgoing_message(self, data: dict) -> None:
        """Parse and deliver a single outgoing envelope to Discord.

        Deserialises the ``payload`` field. Envelopes with
        ``context["atelier"]["streamed"] == True`` are silently dropped: the
        streaming consumer already assembled and delivered the reply.
        If the envelope is a progress event (``ACTION_MESSAGE_PROGRESS``),
        delegates to ``_deliver_progress_event``. For all other final replies,
        cancels typing and sends via Discord API.

        Args:
            data: Raw Redis stream entry fields. Must contain a ``"payload"``
                key with a JSON-serialised ``Envelope``.
        """
        try:
            envelope = Envelope.from_json(data.get("payload", "{}"))
        except (ValueError, KeyError) as exc:
            logger.error("Malformed envelope payload, skipping: %s", exc)
            return

        # LLM reply already delivered via streaming stream — skip to avoid duplicate
        atelier_ctx: AtelierCtx = envelope.context.get(CTX_ATELIER, {})  # type: ignore[assignment]
        if atelier_ctx.get("streamed"):
            logger.debug(
                "Skipping outgoing:discord for %s — already delivered via streaming",
                envelope.correlation_id[:8],
            )
            return

        if envelope.action == ACTION_MESSAGE_PROGRESS:
            channel = await self._resolve_discord_channel(envelope)
            if channel:
                await self._deliver_progress_event(envelope, channel)
            return

        channel = await self._resolve_discord_channel(envelope)
        if not channel:
            return

        preview = envelope.content[:100] + "…" if len(envelope.content) > 100 else envelope.content
        logger.debug(
            "SEND discord | corr=%s | channel=%s | content=%r",
            envelope.correlation_id[:8],
            channel,
            preview,
        )

        self._cancel_typing(envelope.correlation_id)

        if not envelope.content or not envelope.content.strip():
            logger.warning(
                "Skipping Discord send for %s — envelope content is empty or whitespace.",
                envelope.correlation_id,
            )
            return

        for part in _split_discord_message(envelope.content):
            await channel.send(part)

    async def _consume_streaming_reply(self, envelope: Envelope) -> None:
        """Buffer token-by-token chunks from the streaming stream and send once complete.

        Reads from ``relais:messages:streaming:discord:{corr_id}`` via XREAD
        until an entry with ``is_final=1`` is received, then assembles all
        chunks into a single message and delivers it to Discord.

        Args:
            envelope: The original request envelope (for corr_id, reply_to, etc.).
        """
        if self._redis_conn is None:
            logger.error("Redis connection unavailable for streaming reply consumer")
            return

        corr_id = envelope.correlation_id
        stream_key = stream_streaming("discord", corr_id)
        last_id = "0-0"
        buffer: list[str] = []
        deadline = asyncio.get_running_loop().time() + 300

        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    logger.warning("Streaming reply timed out for %s", corr_id[:8])
                    return

                results = await self._redis_conn.xread(
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
                            self._cancel_typing(corr_id)

                            channel = await self._resolve_discord_channel(envelope)
                            if channel and full_text.strip():
                                for part in _split_discord_message(full_text):
                                    await channel.send(part)
                            return
        except Exception as exc:
            logger.error("Streaming reply consumer error for %s: %s", corr_id[:8], exc)

    async def _subscribe_streaming_start(self) -> None:
        """Subscribe to relais:streaming:start:discord and spawn reply consumers.

        Listens on the Pub/Sub channel that Atelier publishes to before starting
        agent execution. For each start signal, the original request envelope is
        parsed and a ``_consume_streaming_reply`` task is spawned.
        """
        if self._redis_conn is None:
            logger.error("Redis connection unavailable for streaming start subscriber")
            return

        pubsub = self._redis_conn.pubsub()
        await pubsub.subscribe(pubsub_streaming_start("discord"))
        logger.info("Subscribed to %s", pubsub_streaming_start("discord"))

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
                    logger.error("Failed to parse streaming start envelope: %s", exc)
                    continue

                task = asyncio.get_running_loop().create_task(
                    self._consume_streaming_reply(envelope)
                )
                self._streaming_tasks.add(task)
                task.add_done_callback(self._streaming_tasks.discard)
        except Exception as exc:
            logger.error("Streaming start subscriber error: %s", exc)

    async def _consume_outgoing_stream(self) -> None:
        """Background task: consume final answers from Atelier and send to Discord.

        Reads from ``relais:messages:outgoing:discord`` via a Redis consumer
        group (at-least-once delivery). Each message is ACKed in a ``finally``
        block after ``_deliver_outgoing_message`` runs, whether delivery
        succeeded or not. This prevents undeliverable messages (e.g. deleted
        Discord channels) from poisoning the PEL indefinitely.

        On outer Redis errors (connection loss, stream errors) the loop sleeps
        1 second before retrying.
        """
        if self._redis_conn is None:
            logger.error("Redis connection not available for outgoing stream consumer")
            return
        await self._ensure_consumer_group(self.stream_out, self.group_name)
        logger.info("Listening for outgoing messages targeted to Discord...")

        while not self.is_closed():
            try:
                results = await self._redis_conn.xreadgroup(
                    self.group_name,
                    self.consumer_name,
                    {self.stream_out: ">"},
                    count=10,
                    block=2000,
                )
                for _, messages in results:
                    for message_id, data in messages:
                        try:
                            await self._deliver_outgoing_message(data)
                        except Exception as exc:
                            logger.error(
                                "Undeliverable Discord message %s, routing to DLQ: %s",
                                message_id,
                                exc,
                            )
                            await self._redis_conn.xadd(
                                STREAM_OUTGOING_FAILED,
                                {
                                    "source": self.stream_out,
                                    "message_id": message_id,
                                    "payload": data.get("payload", ""),
                                    "reason": str(exc),
                                },
                            )
                        finally:
                            await self._redis_conn.xack(
                                self.stream_out, self.group_name, message_id
                            )
            except Exception as exc:
                logger.error("Background stream error: %s", exc)
                await asyncio.sleep(1)
