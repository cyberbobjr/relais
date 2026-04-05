"""Archiviste brick — system-wide observer and audit logger.

Functional role
---------------
Passive observer that records all system events and pipeline messages without
ever blocking or rejecting them.  Writes structured JSONL audit files and
re-emits Redis log entries to the Python logging subsystem for real-time
console visibility.

Technical overview
------------------
``Archiviste`` runs two concurrent asyncio consumer loops:

* ``_process_stream`` — subscribes to the three event streams
  (relais:logs, relais:events:system, relais:events:messages).  Each message
  is serialized to a JSONL file via ``_write_event``; entries arriving on
  ``relais:logs`` are additionally re-emitted through ``logging.getLogger``.
* ``_process_pipeline_streams`` — subscribes to the full pipeline stream list
  (``_PIPELINE_STREAMS``).  Each message is deserialized as an ``Envelope``
  (best-effort) and logged via the ``archiviste.pipeline`` logger for
  end-to-end traceability.

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

# Configure local simple logging for the archivist itself
_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=_log_level,
    format='%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s',
    stream=sys.stdout
)

from common.redis_client import RedisClient
from common.config_loader import get_relais_home
from common.shutdown import GracefulShutdown
from common.envelope import Envelope
logger = logging.getLogger("archiviste")

# Pipeline streams observed by archiviste_pipeline_group.
# Add new channel outgoing streams here as they are introduced.
_PIPELINE_STREAMS: list[str] = [
    "relais:messages:incoming",
    "relais:security",
    "relais:tasks",
    "relais:tasks:failed",
    "relais:messages:outgoing:discord",
]


class Archiviste:
    def __init__(self):
        self.client = RedisClient("archiviste")
        self.base_dir = get_relais_home() / "logs"
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.events_log = self.base_dir / "events.jsonl"
        self.system_log = self.base_dir / "system.log"

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

    async def _process_stream(self, redis_conn, shutdown: GracefulShutdown | None = None):
        """Consume streams using a static consumer group.

        Exits cleanly when ``shutdown.is_stopping()`` returns True.

        Args:
            redis_conn: Active Redis connection.
            shutdown: GracefulShutdown instance controlling the loop lifetime.
                If None a new instance is created (backward-compatible).
        """
        if shutdown is None:
            shutdown = GracefulShutdown()

        group_name = "archiviste_group"
        consumer_name = "archiviste_1"
        streams = {
            "relais:logs": ">",
            "relais:events:system": ">",
            "relais:events:messages": ">"
        }

        # Create consumer group if it doesn't exist
        for stream in streams.keys():
            try:
                await redis_conn.xgroup_create(stream, group_name, mkstream=True)
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    logger.warning(f"Consumer group error for {stream}: {e}")

        logger.info("Archiviste listening to streams...")

        while not shutdown.is_stopping():
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
                        if stream == "relais:logs":
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
                logger.error(f"Error reading from stream: {e}")
                await asyncio.sleep(1)

    async def _process_pipeline_streams(
        self, redis_conn, shutdown: GracefulShutdown | None = None
    ):
        """Consume pipeline streams and log Envelope fields for diagnostics.

        Observes all streams listed in ``_PIPELINE_STREAMS`` via the consumer
        group ``archiviste_pipeline_group``.  Each message is deserialized as
        an ``Envelope`` and logged to the ``archiviste.pipeline`` logger.
        Messages that are not valid Envelopes (e.g. DLQ entries) produce a
        WARNING instead.

        Exits cleanly when ``shutdown.is_stopping()`` returns True.

        Args:
            redis_conn: Active Redis connection.
            shutdown: GracefulShutdown instance controlling the loop lifetime.
                If None a new instance is created (backward-compatible).
        """
        if shutdown is None:
            shutdown = GracefulShutdown()

        pipeline_logger = logging.getLogger("archiviste.pipeline")
        group_name = "archiviste_pipeline_group"
        consumer_name = "archiviste_pipeline_1"

        streams: dict[str, str] = {}
        for stream in _PIPELINE_STREAMS:
            try:
                await redis_conn.xgroup_create(stream, group_name, mkstream=True)
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    logger.warning(f"Consumer group error for {stream}: {e}")
            streams[stream] = ">"

        logger.info("Archiviste pipeline observer started on %d streams", len(streams))

        while not shutdown.is_stopping():
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
                logger.error(f"Pipeline stream error: {e}")
                await asyncio.sleep(1)

    async def start(self):
        """Start the Archiviste service and its main processing loops.

        Launches ``_process_stream`` (relais:logs + events) and
        ``_process_pipeline_streams`` (pipeline traffic) concurrently via
        ``asyncio.gather``.  Registers SIGTERM/SIGINT handlers via
        GracefulShutdown so both loops exit cleanly on termination signals.
        """
        shutdown = GracefulShutdown()
        shutdown.install_signal_handlers()
        redis_conn = await self.client.get_connection()
        try:
            await asyncio.gather(
                self._process_stream(redis_conn, shutdown=shutdown),
                self._process_pipeline_streams(redis_conn, shutdown=shutdown),
            )
        except asyncio.CancelledError:
            logger.info("Archiviste shutting down...")
        finally:
            await self.client.close()
            logger.info("Archiviste stopped gracefully")

if __name__ == "__main__":
    from common.init import initialize_user_dir
    initialize_user_dir()
    archiviste = Archiviste()
    try:
        asyncio.run(archiviste.start())
    except KeyboardInterrupt:
        pass
