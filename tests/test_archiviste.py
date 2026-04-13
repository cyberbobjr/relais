"""Unit tests for archiviste.cleanup_retention."""

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# CleanupManager tests
# ---------------------------------------------------------------------------

from archiviste.cleanup_retention import CleanupManager, RetentionConfig


@pytest.fixture
def archive_dir(tmp_path: Path) -> Path:
    """Return a temporary archive directory."""
    d = tmp_path / "archive"
    d.mkdir()
    return d


@pytest.fixture
def manager(archive_dir: Path) -> CleanupManager:
    """Return a CleanupManager with a 30-day JSONL retention."""
    return CleanupManager(archive_dir=archive_dir, config=RetentionConfig(jsonl_days=30))


def _create_jsonl(directory: Path, name: str, age_days: float) -> Path:
    """Create a .jsonl file whose mtime is *age_days* days in the past."""
    path = directory / name
    path.write_text('{"event": "test"}\n', encoding="utf-8")
    old_mtime = time.time() - age_days * 86400
    import os
    os.utime(path, (old_mtime, old_mtime))
    return path


@pytest.mark.asyncio
async def test_cleanup_jsonl_deletes_old_files(manager: CleanupManager, archive_dir: Path) -> None:
    """cleanup_jsonl() must delete JSONL files older than jsonl_days."""
    old_file = _create_jsonl(archive_dir, "old.jsonl", age_days=60)

    deleted = await manager.cleanup_jsonl()

    assert deleted == 1
    assert not old_file.exists()


@pytest.mark.asyncio
async def test_cleanup_jsonl_keeps_recent_files(manager: CleanupManager, archive_dir: Path) -> None:
    """cleanup_jsonl() must keep recent files."""
    recent_file = _create_jsonl(archive_dir, "recent.jsonl", age_days=5)

    deleted = await manager.cleanup_jsonl()

    assert deleted == 0
    assert recent_file.exists()


@pytest.mark.asyncio
async def test_cleanup_jsonl_returns_correct_count(
    manager: CleanupManager, archive_dir: Path
) -> None:
    """cleanup_jsonl() must return the exact number of deleted files."""
    _create_jsonl(archive_dir, "old1.jsonl", age_days=90)
    _create_jsonl(archive_dir, "old2.jsonl", age_days=45)
    _create_jsonl(archive_dir, "recent.jsonl", age_days=10)

    deleted = await manager.cleanup_jsonl()

    assert deleted == 2


@pytest.mark.asyncio
async def test_get_stats_returns_correct_nb_files_and_total_size(
    manager: CleanupManager, archive_dir: Path
) -> None:
    """get_stats() must return the correct file_count and total_bytes."""
    content = '{"event": "a"}\n'
    f1 = archive_dir / "a.jsonl"
    f2 = archive_dir / "b.jsonl"
    f1.write_text(content, encoding="utf-8")
    f2.write_text(content, encoding="utf-8")

    stats = await manager.get_stats()

    assert stats["file_count"] == 2
    expected_bytes = f1.stat().st_size + f2.stat().st_size
    assert stats["total_bytes"] == expected_bytes


@pytest.mark.asyncio
async def test_get_stats_empty_directory(manager: CleanupManager) -> None:
    """get_stats() must return file_count=0 and total_bytes=0 when the directory is empty."""
    stats = await manager.get_stats()

    assert stats["file_count"] == 0
    assert stats["total_bytes"] == 0
    assert stats["oldest_mtime"] is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_run_daily_deletes_old_files_and_returns_none(
    manager: CleanupManager, archive_dir: Path
) -> None:
    """run_daily() must delete stale files and return None."""
    old_file = _create_jsonl(archive_dir, "stale.jsonl", age_days=60)
    recent_file = _create_jsonl(archive_dir, "fresh.jsonl", age_days=5)

    result = await manager.run_daily()

    assert result is None
    assert not old_file.exists()
    assert recent_file.exists()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_run_daily_on_empty_directory_does_not_raise(
    manager: CleanupManager,
) -> None:
    """run_daily() on an empty directory must not raise an exception."""
    result = await manager.run_daily()
    assert result is None


# ---------------------------------------------------------------------------
# Archiviste main.py tests
# ---------------------------------------------------------------------------


def _make_redis_conn() -> AsyncMock:
    """Build a fully-mocked async Redis connection."""
    conn = AsyncMock()
    conn.xgroup_create = AsyncMock(return_value=True)
    conn.xreadgroup = AsyncMock(return_value=[])
    conn.xack = AsyncMock(return_value=1)
    return conn


def _make_archiviste(tmp_path: Path) -> "Archiviste":
    """Instantiate Archiviste with RELAIS_HOME redirected to *tmp_path*."""
    from archiviste.main import Archiviste

    with patch.dict(os.environ, {"RELAIS_HOME": str(tmp_path)}):
        arc = Archiviste()
    return arc


@pytest.mark.unit
def test_archiviste_init_creates_log_dir(tmp_path: Path) -> None:
    """Archiviste.__init__ must create the logs directory under RELAIS_HOME."""
    from archiviste.main import Archiviste

    with patch.dict(os.environ, {"RELAIS_HOME": str(tmp_path)}):
        arc = Archiviste()

    assert (tmp_path / "logs").is_dir()
    assert arc.events_log == tmp_path / "logs" / "events.jsonl"
    assert arc.system_log == tmp_path / "logs" / "system.log"


@pytest.mark.unit
def test_write_event_appends_jsonl(tmp_path: Path) -> None:
    """_write_event must append a valid JSON line to events.jsonl."""
    arc = _make_archiviste(tmp_path)

    arc._write_event("1234-0", "relais:logs", {"message": "hello", "level": "INFO"})

    lines = arc.events_log.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["ts"] == "1234-0"
    assert record["stream"] == "relais:logs"
    assert record["data"]["message"] == "hello"


@pytest.mark.unit
def test_write_event_appends_multiple_lines(tmp_path: Path) -> None:
    """_write_event called twice produces exactly two JSONL lines."""
    arc = _make_archiviste(tmp_path)

    arc._write_event("1-0", "relais:logs", {"msg": "first"})
    arc._write_event("2-0", "relais:logs", {"msg": "second"})

    lines = arc.events_log.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["data"]["msg"] == "first"
    assert json.loads(lines[1])["data"]["msg"] == "second"


@pytest.mark.unit
def test_write_event_handles_io_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """_write_event must log an error and not raise when the file cannot be written."""
    import logging

    arc = _make_archiviste(tmp_path)
    # Point events_log to a path that cannot be opened (parent is a file)
    blocker = tmp_path / "logs" / "blocker"
    blocker.write_text("x")
    arc.events_log = blocker / "events.jsonl"  # parent is a file → OSError

    with caplog.at_level(logging.ERROR, logger="archiviste"):
        arc._write_event("9-0", "relais:logs", {"msg": "bad"})

    assert any("Failed to write event" in r.message for r in caplog.records)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_stream_creates_consumer_groups(tmp_path: Path) -> None:
    """_process_stream must call xgroup_create for every monitored stream."""
    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    # Let the loop run one iteration then cancel.
    conn.xreadgroup.side_effect = [[], asyncio.CancelledError()]

    event = asyncio.Event()
    with pytest.raises(asyncio.CancelledError):
        await arc._process_stream(conn, event)

    expected_streams = {"relais:logs", "relais:events:system", "relais:events:messages"}
    created_streams = {c.args[0] for c in conn.xgroup_create.await_args_list}
    assert created_streams == expected_streams


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_stream_busygroup_error_is_silenced(tmp_path: Path) -> None:
    """BUSYGROUP error during xgroup_create must be silently ignored."""
    from redis.exceptions import ResponseError

    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    busygroup_exc = ResponseError("BUSYGROUP Consumer Group name already exists")
    conn.xgroup_create.side_effect = busygroup_exc
    conn.xreadgroup.side_effect = asyncio.CancelledError()

    # Should not raise despite xgroup_create always raising BUSYGROUP.
    event = asyncio.Event()
    with pytest.raises(asyncio.CancelledError):
        await arc._process_stream(conn, event)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_stream_non_busygroup_error_raises(
    tmp_path: Path,
) -> None:
    """Non-BUSYGROUP errors from xgroup_create must propagate (not be silenced)."""
    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    conn.xgroup_create.side_effect = Exception("Some other Redis error")

    event = asyncio.Event()
    with pytest.raises(Exception, match="Some other Redis error"):
        await arc._process_stream(conn, event)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_stream_writes_event_and_acks(tmp_path: Path) -> None:
    """Messages returned by xreadgroup are written to JSONL and acknowledged."""
    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    stream_name = "relais:events:messages"
    message_id = "1000-0"
    message_data = {"content": "test-event", "sender_id": "u1"}

    # First call returns one message; second call cancels the loop.
    conn.xreadgroup.side_effect = [
        [(stream_name, [(message_id, message_data)])],
        asyncio.CancelledError(),
    ]

    event = asyncio.Event()
    with pytest.raises(asyncio.CancelledError):
        await arc._process_stream(conn, event)

    # JSONL written
    lines = arc.events_log.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["ts"] == message_id
    assert record["data"]["content"] == "test-event"

    # Message acknowledged
    conn.xack.assert_awaited_once_with(stream_name, "archiviste_group", message_id)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_stream_logs_stream_log_to_stdout(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Messages on relais:logs must be logged with level and brick."""
    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    conn.xreadgroup.side_effect = [
        [("relais:logs", [("500-0", {"message": "hello world", "level": "DEBUG", "brick": "portail"})])],
        asyncio.CancelledError(),
    ]

    import logging
    event = asyncio.Event()
    with caplog.at_level(logging.DEBUG), pytest.raises(asyncio.CancelledError):
        await arc._process_stream(conn, event)

    assert "hello world" in caplog.text
    assert "portail" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_stream_multiple_messages_in_sequence(tmp_path: Path) -> None:
    """Multiple messages across streams are all written and acknowledged."""
    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    conn.xreadgroup.side_effect = [
        [
            ("relais:logs", [("1-0", {"message": "m1", "level": "INFO", "brick": "a"})]),
            ("relais:events:system", [("2-0", {"event": "startup"})]),
        ],
        asyncio.CancelledError(),
    ]

    event = asyncio.Event()
    with pytest.raises(asyncio.CancelledError):
        await arc._process_stream(conn, event)

    lines = arc.events_log.read_text().splitlines()
    assert len(lines) == 2
    assert conn.xack.await_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_stream_xreadgroup_error_sleeps_and_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A transient xreadgroup error must be logged and the loop must continue."""
    import logging

    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    conn.xreadgroup.side_effect = [
        Exception("connection lost"),
        asyncio.CancelledError(),
    ]

    event = asyncio.Event()
    with caplog.at_level(logging.ERROR, logger="archiviste"):
        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            with pytest.raises(asyncio.CancelledError):
                await arc._process_stream(conn, event)

    assert any("Error reading from stream" in r.message for r in caplog.records)
    mock_sleep.assert_awaited_once_with(1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_calls_get_connection_and_process_stream(tmp_path: Path) -> None:
    """start() must obtain a Redis connection and run _process_stream."""
    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    mock_client = AsyncMock()
    mock_client.get_connection = AsyncMock(return_value=conn)
    mock_client.close = AsyncMock()

    mock_shutdown = MagicMock()
    mock_shutdown.stop_event = asyncio.Event()

    arc._process_stream = AsyncMock(side_effect=asyncio.CancelledError())
    arc._process_pipeline_streams = AsyncMock(return_value=None)

    with patch("archiviste.main.GracefulShutdown", return_value=mock_shutdown):
        with patch("archiviste.main.RedisClient", return_value=mock_client):
            await arc.start()

    mock_client.get_connection.assert_awaited_once()
    arc._process_stream.assert_awaited_once_with(conn, ANY)
    mock_client.close.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_closes_redis_on_normal_completion(tmp_path: Path) -> None:
    """start() must close the Redis connection in the finally block."""
    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    mock_client = AsyncMock()
    mock_client.get_connection = AsyncMock(return_value=conn)
    mock_client.close = AsyncMock()

    mock_shutdown = MagicMock()
    mock_shutdown.stop_event = asyncio.Event()

    arc._process_stream = AsyncMock(return_value=None)
    arc._process_pipeline_streams = AsyncMock(return_value=None)

    with patch("archiviste.main.GracefulShutdown", return_value=mock_shutdown):
        with patch("archiviste.main.RedisClient", return_value=mock_client):
            await arc.start()

    mock_client.close.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_stream_empty_xreadgroup_result_does_not_write(tmp_path: Path) -> None:
    """An empty result from xreadgroup must produce no JSONL output."""
    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    conn.xreadgroup.side_effect = [
        [],  # empty result
        asyncio.CancelledError(),
    ]

    event = asyncio.Event()
    with pytest.raises(asyncio.CancelledError):
        await arc._process_stream(conn, event)

    assert not arc.events_log.exists() or arc.events_log.read_text() == ""
    conn.xack.assert_not_awaited()


# ---------------------------------------------------------------------------
# Axe A — _process_pipeline_streams tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pipeline_streams_logs_envelope_fields(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """_process_pipeline_streams must log correlation_id and sender_id from Envelope.

    When xreadgroup returns a message whose data contains a valid serialised
    Envelope under the 'payload' key, the archiviste.pipeline logger must emit
    an INFO record that includes:
    - the first 8 chars of the correlation_id
    - the sender_id
    - the stream name
    - a content preview
    """
    import logging
    from common.envelope import Envelope
    from common.envelope_actions import ACTION_MESSAGE_INCOMING

    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    envelope = Envelope(
        content="Bonjour, peux-tu m'aider avec quelque chose de précis ?",
        sender_id="discord:805123456789",
        channel="discord",
        session_id="sess-abc",
        correlation_id="9b8ddb16-0000-0000-0000-000000000000",
        action=ACTION_MESSAGE_INCOMING,
    )
    envelope.add_trace("portail", "received")
    envelope.add_trace("sentinelle", "ACL verified")

    message_data = {"payload": envelope.to_json()}
    stream_name = "relais:tasks"
    message_id = "2000-0"

    # First call returns one pipeline message; second exits the loop.
    conn.xreadgroup.side_effect = [
        [(stream_name, [(message_id, message_data)])],
        asyncio.CancelledError(),
    ]

    with caplog.at_level(logging.INFO, logger="archiviste.pipeline"):
        with pytest.raises(asyncio.CancelledError):
            await arc._process_pipeline_streams(conn, asyncio.Event())

    pipeline_records = [r for r in caplog.records if r.name == "archiviste.pipeline"]
    assert pipeline_records, "archiviste.pipeline must emit at least one log record"

    combined = " ".join(r.getMessage() for r in pipeline_records)
    assert "9b8ddb16" in combined, "First 8 chars of correlation_id expected in log"
    assert "discord:805123456789" in combined, "sender_id expected in log"
    assert stream_name in combined, "stream name expected in log"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pipeline_streams_handles_non_envelope_gracefully(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-Envelope payloads (e.g. DLQ messages) must produce a WARNING log.

    DLQ messages in relais:tasks:failed contain 'reason' and 'failed_at' fields
    rather than a 'payload' key holding a valid Envelope.  The archiviste must
    log a WARNING instead of raising an exception.
    """
    import logging

    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    dlq_data = {
        "reason": "SDKExecutionError: model unavailable",
        "failed_at": "1711756332.0",
    }
    stream_name = "relais:tasks:failed"
    message_id = "3000-0"

    conn.xreadgroup.side_effect = [
        [(stream_name, [(message_id, dlq_data)])],
        asyncio.CancelledError(),
    ]

    with caplog.at_level(logging.WARNING, logger="archiviste.pipeline"):
        with pytest.raises(asyncio.CancelledError):
            await arc._process_pipeline_streams(conn, asyncio.Event())

    warning_records = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and r.name == "archiviste.pipeline"
    ]
    assert warning_records, "A WARNING record must be emitted for non-Envelope DLQ messages"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pipeline_streams_exits_on_shutdown(tmp_path: Path) -> None:
    """_process_pipeline_streams must return immediately when shutdown signals stop."""
    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    event = asyncio.Event()
    event.set()  # pre-set → loop body never executes

    await arc._process_pipeline_streams(conn, event)

    # xreadgroup must never have been called
    conn.xreadgroup.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_pipeline_streams_creates_consumer_groups(tmp_path: Path) -> None:
    """_process_pipeline_streams must call xgroup_create for every pipeline stream."""
    from archiviste.main import _PIPELINE_STREAMS

    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    # xgroup_create runs before the loop; pre-set event so loop exits immediately.
    event = asyncio.Event()
    event.set()
    conn.xreadgroup.return_value = []

    await arc._process_pipeline_streams(conn, event)

    created_streams = {c.args[0] for c in conn.xgroup_create.await_args_list}
    for stream in _PIPELINE_STREAMS:
        assert stream in created_streams, f"{stream} must have a consumer group created"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_runs_process_stream_and_pipeline_streams_in_parallel(
    tmp_path: Path,
) -> None:
    """start() must launch _process_stream and _process_pipeline_streams concurrently."""
    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    mock_client = AsyncMock()
    mock_client.get_connection = AsyncMock(return_value=conn)
    mock_client.close = AsyncMock()

    mock_shutdown = MagicMock()
    mock_shutdown.stop_event = asyncio.Event()

    arc._process_stream = AsyncMock(return_value=None)
    arc._process_pipeline_streams = AsyncMock(return_value=None)

    with patch("archiviste.main.GracefulShutdown", return_value=mock_shutdown):
        with patch("archiviste.main.RedisClient", return_value=mock_client):
            await arc.start()

    arc._process_stream.assert_awaited_once()
    arc._process_pipeline_streams.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_stream_reemit_includes_correlation_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """relais:logs messages with correlation_id must include it in the re-emitted log line."""
    import logging

    arc = _make_archiviste(tmp_path)
    conn = _make_redis_conn()

    conn.xreadgroup.side_effect = [
        [(
            "relais:logs",
            [(
                "600-0",
                {
                    "message": "Forwarded to sentinelle",
                    "level": "INFO",
                    "brick": "portail",
                    "correlation_id": "abcd1234-0000-0000-0000-000000000000",
                    "sender_id": "discord:99999",
                },
            )],
        )],
        asyncio.CancelledError(),
    ]

    event = asyncio.Event()
    with caplog.at_level(logging.INFO, logger="portail"):
        with pytest.raises(asyncio.CancelledError):
            await arc._process_stream(conn, event)

    assert any(
        "abcd1234" in r.getMessage() for r in caplog.records if r.name == "portail"
    ), "correlation_id prefix must appear in the re-emitted log record"
