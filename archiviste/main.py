"""Archiviste brick — system-wide observer and audit logger.

Functional role
---------------
Passive observer that records all system events and pipeline messages without
ever blocking or rejecting them.  Writes structured JSONL audit files and
re-emits Redis log entries to the Python logging subsystem for real-time
console visibility.

Technical overview
------------------
``Archiviste`` extends :class:`~common.brick_base.BrickBase` and runs two
concurrent asyncio consumer loops:

* ``_process_stream`` — subscribes to the three event streams
  (relais:logs, relais:events:system, relais:events:messages).  Each message
  is serialized to a JSONL file via ``_write_event``; entries arriving on
  ``relais:logs`` are additionally re-emitted through ``logging.getLogger``.
* ``_process_pipeline_streams`` — subscribes to the full pipeline stream list
  (``_PIPELINE_STREAMS``).  Each message is deserialized as an ``Envelope``
  (best-effort) and logged via the ``archiviste.pipeline`` logger for
  end-to-end traceability.

Both loops use a shared ``asyncio.Event`` (``shutdown_event``) provided by
``BrickBase.start()`` so they exit cleanly on SIGTERM/SIGINT.  The event
streams contain raw dicts (not Envelopes), so they are handled outside the
standard BrickBase ``_run_stream_loop``; the pipeline streams contain full
Envelope payloads which are deserialized inline.

Output files:
  - ``~/.relais/logs/events.jsonl``  — all event stream entries
  - supervisord stdout / ``~/.relais/logs/archiviste.log``  — pipeline traces

Redis channels
--------------
Consumed (event streams):
  - relais:logs               (consumer group: archiviste_group)
  - relais:events:system      (consumer group: archiviste_group)
  - relais:events:messages    (consumer group: archiviste_group)

Consumed (pipeline observation):
  - relais:messages:incoming          (consumer group: archiviste_pipeline_group)
  - relais:security                   (consumer group: archiviste_pipeline_group)
  - relais:tasks                      (consumer group: archiviste_pipeline_group)
  - relais:tasks:failed               (consumer group: archiviste_pipeline_group)
  - relais:messages:outgoing:discord  (consumer group: archiviste_pipeline_group)
  (add new outgoing streams to _PIPELINE_STREAMS as channels are introduced)

Processing flow — event loop
----------------------------
  (1) Consume from relais:logs + relais:events:* (archiviste_group).
  (2) Write raw event dict to events.jsonl via _write_event.
  (3) If stream is relais:logs: re-emit via Python logging.
  (4) XACK.

Processing flow — pipeline loop
--------------------------------
  (1) Consume from each stream in _PIPELINE_STREAMS (archiviste_pipeline_group).
  (2) Attempt Envelope deserialization (log raw data on failure).
  (3) Log envelope fields (correlation_id, sender_id, channel, action, traces).
  (4) XACK.

XACK contract:
  - Both loops ACK unconditionally — Archiviste must never stall the pipeline.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime

from common.brick_base import BrickBase, StreamSpec, configure_logging_once
from common.config_loader import get_relais_home
from common.envelope import Envelope
from common.redis_client import RedisClient
from common.shutdown import GracefulShutdown
from common.streams import (
    STREAM_EVENTS_MESSAGES,
    STREAM_EVENTS_SYSTEM,
    STREAM_LOGS,
    STREAM_INCOMING,
    STREAM_SECURITY,
    STREAM_TASKS,
    STREAM_TASKS_FAILED,
    stream_outgoing,
)
logger = logging.getLogger("archiviste")

# Pipeline streams observed by archiviste_pipeline_group.
# Add new channel outgoing streams here as they are introduced.
_PIPELINE_STREAMS: list[str] = [
    STREAM_INCOMING,
    STREAM_SECURITY,
    STREAM_TASKS,
    STREAM_TASKS_FAILED,
    stream_outgoing("discord"),
]


class Archiviste(BrickBase):
    def __init__(self):
        super().__init__("archiviste")
        self.base_dir = get_relais_home() / "logs"
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.events_log = self.base_dir / "events.jsonl"
        self.system_log = self.base_dir / "system.log"
        self._load()

    # ------------------------------------------------------------------
    # BrickBase abstract interface
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """No-op: Archiviste has no YAML configuration."""

    def stream_specs(self) -> list[StreamSpec]:
        """Archiviste uses raw-message loops; no standard StreamSpecs required."""
        return []

    # ------------------------------------------------------------------
    # BrickBase lifecycle hooks
    # ------------------------------------------------------------------

    def _create_shutdown(self) -> GracefulShutdown:
        return GracefulShutdown()

    # ------------------------------------------------------------------
    # Raw-message consumer loops
    # ------------------------------------------------------------------

    def _write_event(self, timestamp: str, stream: bytes, message: dict):
        """Append event to the JSONL ledger."""
        try:
            record = {
                "ts": timestamp,
                "stream": stream,
                "data": {k: v for k, v in message.items()}
            }
            with open(self.events_log, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.error(f"Failed to write event: {e}")

    async def _process_stream(
        self, redis_conn, shutdown_event: asyncio.Event
    ):
        """Consume event streams (relais:logs + relais:events:*).

        Exits cleanly when ``shutdown_event`` is set.

        Args:
            redis_conn: Active Redis connection.
            shutdown_event: asyncio.Event controlling the loop lifetime.
        """
        group_name = "archiviste_group"
        consumer_name = "archiviste_1"
        streams = {
            STREAM_LOGS: ">",
            STREAM_EVENTS_SYSTEM: ">",
            STREAM_EVENTS_MESSAGES: ">",
        }

        # Create consumer group if it doesn't exist
        for stream in streams.keys():
            try:
                await redis_conn.xgroup_create(stream, group_name, mkstream=True)
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    await self.log.warning(f"Consumer group error for {stream}: {e}")

        # Emit the startup log *after* the consumer group exists so this
        # brick's own archiviste_group will receive the message.
        await redis_conn.xadd(STREAM_LOGS, {
            "level": "INFO",
            "brick": "archiviste",
            "message": "Archiviste started",
        })

        await self.log.info("Archiviste listening to streams...")

        while not shutdown_event.is_set():
            try:
                # Block for 2 seconds waiting for new events
                results = await redis_conn.xreadgroup(
                    group_name,
                    consumer_name,
                    streams,
                    count=50,
                    block=2000
                )

                for stream, messages in results:
                    for message_id, data in messages:
                        self._write_event(message_id, stream, data)
                        # Acknowledge the message so it's removed from PEL
                        await redis_conn.xack(stream, group_name, message_id)

                        # Re-emit system logs via the standard logger so they use the unified format
                        if stream == STREAM_LOGS:
                            msg = data.get("message", "")
                            level = data.get("level", "INFO").upper()
                            brick = data.get("brick", "unknown")
                            cid = data.get("correlation_id", "")
                            sid = data.get("sender_id", "")
                            if level == "WARN":
                                level = "WARNING"
                            numeric_level = logging.getLevelName(level)
                            if not isinstance(numeric_level, int):
                                numeric_level = logging.INFO
                            prefix = f"[{cid[:8]}] {sid} | " if cid else ""
                            logging.getLogger(brick).log(numeric_level, f"{prefix}{msg}")

            except Exception as e:
                await self.log.error(f"Error reading from stream: {e}")
                await asyncio.sleep(1)

    async def _process_pipeline_streams(
        self, redis_conn, shutdown_event: asyncio.Event
    ):
        """Consume pipeline streams and log Envelope fields for diagnostics.

        Observes all streams listed in ``_PIPELINE_STREAMS`` via the consumer
        group ``archiviste_pipeline_group``.  Each message is deserialized as
        an ``Envelope`` and logged to the ``archiviste.pipeline`` logger.
        Messages that are not valid Envelopes (e.g. DLQ entries) produce a
        WARNING instead.

        Exits cleanly when ``shutdown_event`` is set.

        Args:
            redis_conn: Active Redis connection.
            shutdown_event: asyncio.Event controlling the loop lifetime.
        """
        pipeline_logger = logging.getLogger("archiviste.pipeline")
        group_name = "archiviste_pipeline_group"
        consumer_name = "archiviste_pipeline_1"

        streams: dict[str, str] = {}
        for stream in _PIPELINE_STREAMS:
            try:
                await redis_conn.xgroup_create(stream, group_name, mkstream=True)
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    await self.log.warning(f"Consumer group error for {stream}: {e}")
            streams[stream] = ">"

        await self.log.info(f"Archiviste pipeline observer started on {len(streams)} streams")

        while not shutdown_event.is_set():
            try:
                results = await redis_conn.xreadgroup(
                    group_name,
                    consumer_name,
                    streams,
                    count=50,
                    block=2000,
                )

                for stream_name, messages in results:
                    for message_id, data in messages:
                        self._write_event(message_id, stream_name, data)
                        await redis_conn.xack(stream_name, group_name, message_id)

                        # Attempt to deserialize as Envelope
                        raw_payload = data.get("payload")
                        if raw_payload:
                            try:
                                envelope = Envelope.from_json(
                                    raw_payload if isinstance(raw_payload, str)
                                    else raw_payload.decode()
                                )
                                traces_list = envelope.traces
                                trace_str = ">".join(
                                    t.get("brick", "") for t in traces_list
                                ) if traces_list else ""
                                content_preview = (envelope.content or "")[:60]
                                cid_short = (envelope.correlation_id or "")[:8]
                                pipeline_logger.info(
                                    "[%s] %s → %s | traces=%s | \"%s\"",
                                    cid_short,
                                    envelope.sender_id,
                                    stream_name,
                                    trace_str,
                                    content_preview,
                                )
                            except Exception as parse_exc:
                                pipeline_logger.warning(
                                    "Non-Envelope payload in %s (msg=%s): %s | raw=%r",
                                    stream_name,
                                    message_id,
                                    parse_exc,
                                    str(raw_payload)[:120],
                                )
                        else:
                            # DLQ or other non-Envelope message (no 'payload' key)
                            pipeline_logger.warning(
                                "Non-Envelope message in %s (msg=%s): fields=%s",
                                stream_name,
                                message_id,
                                list(data.keys()),
                            )

            except Exception as e:
                await self.log.error(f"Pipeline stream error: {e}")
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Entry point — override to run raw-message loops alongside BrickBase infra
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start Archiviste using BrickBase infrastructure.

        Overrides ``BrickBase.start()`` because the event streams carry raw
        dicts (not Envelopes) and cannot use ``_run_stream_loop``.  The
        logging-configuration guard from BrickBase is honoured so that
        ``basicConfig`` is never called twice in the same process.
        """
        configure_logging_once()

        shutdown = GracefulShutdown()
        shutdown.install_signal_handlers()
        self.client = RedisClient("archiviste")
        redis_conn = await self.client.get_connection()

        shutdown_event = shutdown.stop_event
        try:
            await asyncio.gather(
                self._process_stream(redis_conn, shutdown_event),
                self._process_pipeline_streams(redis_conn, shutdown_event),
            )
        except asyncio.CancelledError:
            await self.log.info("Archiviste shutting down...")
        finally:
            await self.client.close()
            await self.log.info("Archiviste stopped gracefully")

if __name__ == "__main__":
    from common.init import initialize_user_dir
    initialize_user_dir()
    archiviste = Archiviste()
    try:
        asyncio.run(archiviste.start())
    except KeyboardInterrupt:
        pass
