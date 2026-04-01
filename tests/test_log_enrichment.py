"""Tests for Axe B — enriched Redis log payloads emitted by bricks.

Verifies that:
- portail, sentinelle, atelier, souvenir include correlation_id and sender_id
  in the xadd("relais:logs", ...) payloads.
- Archiviste re-emits those logs with the correlation_id prefix in the
  formatted Python logger message.
"""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch, call

import pytest

from common.envelope import Envelope
from common.user_registry import UserRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(
    content: str = "Hello RELAIS",
    sender_id: str = "discord:111222333",
    channel: str = "discord",
    session_id: str = "sess-test",
    correlation_id: str = "deadbeef-0000-0000-0000-000000000000",
) -> Envelope:
    """Build a test Envelope with predictable IDs."""
    return Envelope(
        content=content,
        sender_id=sender_id,
        channel=channel,
        session_id=session_id,
        correlation_id=correlation_id,
    )


def _make_redis_conn() -> AsyncMock:
    """Return a fully-mocked async Redis connection."""
    conn = AsyncMock()
    conn.xgroup_create = AsyncMock(return_value=True)
    conn.xreadgroup = AsyncMock(return_value=[])
    conn.xack = AsyncMock(return_value=1)
    conn.xadd = AsyncMock(return_value=b"1234-0")
    conn.hset = AsyncMock(return_value=1)
    conn.expire = AsyncMock(return_value=True)
    conn.get = AsyncMock(return_value=None)  # DND inactive by default
    return conn


def _get_xadd_calls_to_logs(conn: AsyncMock) -> list[dict]:
    """Extract all payloads sent to 'relais:logs' via xadd."""
    payloads = []
    for c in conn.xadd.call_args_list:
        args = c.args if c.args else ()
        kwargs = c.kwargs if c.kwargs else {}
        stream = args[0] if args else kwargs.get("name", "")
        if stream == "relais:logs":
            data = args[1] if len(args) > 1 else kwargs.get("fields", {})
            payloads.append(data)
    return payloads


# ---------------------------------------------------------------------------
# Portail — xadd enrichment
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_xadd_includes_correlation_id(tmp_path: Path) -> None:
    """Portail must include correlation_id in the relais:logs xadd payload."""
    from portail.main import Portail

    portail = Portail.__new__(Portail)
    portail.client = AsyncMock()
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"
    portail._dnd_cached = None
    portail._dnd_cache_at = 0.0
    portail._user_registry = UserRegistry(config_path=Path("/nonexistent/users.yaml"))

    envelope = _make_envelope()
    conn = _make_redis_conn()

    # Simulate one message in the stream then stop
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]
    conn.xreadgroup.return_value = [
        (
            "relais:messages:incoming",
            [("1000-0", {"payload": envelope.to_json()})],
        )
    ]

    await portail._process_stream(conn, shutdown)

    log_payloads = _get_xadd_calls_to_logs(conn)
    assert log_payloads, "Portail must xadd at least one entry to relais:logs"

    # The forward-success log must include correlation_id
    success_logs = [p for p in log_payloads if p.get("level") == "INFO"]
    assert any(
        p.get("correlation_id") == envelope.correlation_id for p in success_logs
    ), f"Expected correlation_id={envelope.correlation_id!r} in one of: {success_logs}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_xadd_includes_sender_id(tmp_path: Path) -> None:
    """Portail must include sender_id in the relais:logs xadd payload."""
    from portail.main import Portail

    portail = Portail.__new__(Portail)
    portail.client = AsyncMock()
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"
    portail._dnd_cached = None
    portail._dnd_cache_at = 0.0
    portail._user_registry = UserRegistry(config_path=Path("/nonexistent/users.yaml"))

    envelope = _make_envelope(sender_id="discord:987654321")
    conn = _make_redis_conn()

    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]
    conn.xreadgroup.return_value = [
        (
            "relais:messages:incoming",
            [("1001-0", {"payload": envelope.to_json()})],
        )
    ]

    await portail._process_stream(conn, shutdown)

    log_payloads = _get_xadd_calls_to_logs(conn)
    success_logs = [p for p in log_payloads if p.get("level") == "INFO"]
    assert any(
        p.get("sender_id") == "discord:987654321" for p in success_logs
    ), f"Expected sender_id in one of: {success_logs}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_error_xadd_includes_correlation_id(tmp_path: Path) -> None:
    """Portail error logs must include correlation_id when envelope is available."""
    from portail.main import Portail

    portail = Portail.__new__(Portail)
    portail.client = AsyncMock()
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"
    portail._user_registry = UserRegistry(config_path=Path("/nonexistent/users.yaml"))

    conn = _make_redis_conn()
    # Forward xadd succeeds but raising an error mid-processing by injecting
    # a malformed payload (not valid JSON)
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]
    conn.xreadgroup.return_value = [
        (
            "relais:messages:incoming",
            [("1002-0", {"payload": "INVALID JSON {{{"})],
        )
    ]

    await portail._process_stream(conn, shutdown)

    log_payloads = _get_xadd_calls_to_logs(conn)
    error_logs = [p for p in log_payloads if p.get("level") == "ERROR"]
    assert error_logs, "Portail must xadd an ERROR entry to relais:logs on malformed payload"
    # correlation_id should be empty string (no envelope) — field must exist
    for err in error_logs:
        assert "correlation_id" in err, f"ERROR log must have correlation_id key: {err}"


# ---------------------------------------------------------------------------
# Sentinelle — xadd enrichment
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinelle_xadd_includes_correlation_id(tmp_path: Path) -> None:
    """Sentinelle must include correlation_id in the relais:logs xadd payload."""
    from sentinelle.main import Sentinelle

    sentinelle = Sentinelle.__new__(Sentinelle)
    sentinelle.client = AsyncMock()
    sentinelle.stream_in = "relais:security"
    sentinelle.stream_out = "relais:tasks"
    sentinelle.group_name = "sentinelle_group"
    sentinelle.consumer_name = "sentinelle_1"
    sentinelle._acl = MagicMock()
    sentinelle._acl.is_allowed.return_value = True
    sentinelle._acl.unknown_user_policy = "deny"

    envelope = _make_envelope()
    conn = _make_redis_conn()

    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]
    conn.xreadgroup.return_value = [
        ("relais:security", [("2000-0", {"payload": envelope.to_json()})])
    ]

    await sentinelle._process_stream(conn, shutdown)

    log_payloads = _get_xadd_calls_to_logs(conn)
    info_logs = [p for p in log_payloads if p.get("level") == "INFO"]
    assert any(
        p.get("correlation_id") == envelope.correlation_id for p in info_logs
    ), f"Expected correlation_id in one of: {info_logs}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinelle_xadd_includes_sender_id(tmp_path: Path) -> None:
    """Sentinelle must include sender_id in the relais:logs xadd payload."""
    from sentinelle.main import Sentinelle

    sentinelle = Sentinelle.__new__(Sentinelle)
    sentinelle.client = AsyncMock()
    sentinelle.stream_in = "relais:security"
    sentinelle.stream_out = "relais:tasks"
    sentinelle.group_name = "sentinelle_group"
    sentinelle.consumer_name = "sentinelle_1"
    sentinelle._acl = MagicMock()
    sentinelle._acl.is_allowed.return_value = True
    sentinelle._acl.unknown_user_policy = "deny"

    envelope = _make_envelope(sender_id="telegram:55566677")
    conn = _make_redis_conn()

    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]
    conn.xreadgroup.return_value = [
        ("relais:security", [("2001-0", {"payload": envelope.to_json()})])
    ]

    await sentinelle._process_stream(conn, shutdown)

    log_payloads = _get_xadd_calls_to_logs(conn)
    info_logs = [p for p in log_payloads if p.get("level") == "INFO"]
    assert any(
        p.get("sender_id") == "telegram:55566677" for p in info_logs
    ), f"Expected sender_id in one of: {info_logs}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinelle_xadd_includes_content_preview(tmp_path: Path) -> None:
    """Sentinelle must include content_preview (max 60 chars) in the log payload."""
    from sentinelle.main import Sentinelle

    sentinelle = Sentinelle.__new__(Sentinelle)
    sentinelle.client = AsyncMock()
    sentinelle.stream_in = "relais:security"
    sentinelle.stream_out = "relais:tasks"
    sentinelle.group_name = "sentinelle_group"
    sentinelle.consumer_name = "sentinelle_1"
    sentinelle._acl = MagicMock()
    sentinelle._acl.is_allowed.return_value = True
    sentinelle._acl.unknown_user_policy = "deny"

    long_content = "A" * 100
    envelope = _make_envelope(content=long_content)
    conn = _make_redis_conn()

    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]
    conn.xreadgroup.return_value = [
        ("relais:security", [("2002-0", {"payload": envelope.to_json()})])
    ]

    await sentinelle._process_stream(conn, shutdown)

    log_payloads = _get_xadd_calls_to_logs(conn)
    info_logs = [p for p in log_payloads if p.get("level") == "INFO" and "content_preview" in p]
    assert info_logs, "Sentinelle must include content_preview in log payload"
    preview = info_logs[0]["content_preview"]
    assert len(preview) <= 60, f"content_preview must be at most 60 chars, got {len(preview)}"


# ---------------------------------------------------------------------------
# Archiviste — re-emission with correlation_id prefix
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_archiviste_reemit_includes_correlation_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """_process_stream re-emits relais:logs messages with correlation_id prefix.

    When the log Redis entry contains a 'correlation_id', the Python logger
    message forwarded to stdout must include the [cid[:8]] prefix.
    """
    import logging
    from archiviste.main import Archiviste

    with patch.dict(os.environ, {"RELAIS_HOME": str(tmp_path)}):
        arc = Archiviste()

    conn = AsyncMock()
    conn.xgroup_create = AsyncMock(return_value=True)
    conn.xack = AsyncMock(return_value=1)

    conn.xreadgroup.side_effect = [
        [(
            "relais:logs",
            [(
                "700-0",
                {
                    "message": "Approved to atelier",
                    "level": "INFO",
                    "brick": "sentinelle",
                    "correlation_id": "feedcafe-0000-0000-0000-000000000000",
                    "sender_id": "discord:12345",
                },
            )],
        )],
        asyncio.CancelledError(),
    ]

    with caplog.at_level(logging.INFO, logger="sentinelle"):
        with pytest.raises(asyncio.CancelledError):
            await arc._process_stream(conn)

    assert any(
        "feedcafe" in r.getMessage() for r in caplog.records if r.name == "sentinelle"
    ), "The re-emitted log record must contain the first 8 chars of correlation_id"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_archiviste_reemit_without_correlation_id_no_prefix(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """_process_stream re-emits relais:logs messages without prefix if no correlation_id."""
    import logging
    from archiviste.main import Archiviste

    with patch.dict(os.environ, {"RELAIS_HOME": str(tmp_path)}):
        arc = Archiviste()

    conn = AsyncMock()
    conn.xgroup_create = AsyncMock(return_value=True)
    conn.xack = AsyncMock(return_value=1)

    conn.xreadgroup.side_effect = [
        [(
            "relais:logs",
            [(
                "800-0",
                {
                    "message": "Portail started",
                    "level": "INFO",
                    "brick": "portail",
                },
            )],
        )],
        asyncio.CancelledError(),
    ]

    with caplog.at_level(logging.INFO, logger="portail"):
        with pytest.raises(asyncio.CancelledError):
            await arc._process_stream(conn)

    portail_records = [r for r in caplog.records if r.name == "portail"]
    assert portail_records, "portail logger must receive the re-emitted record"
    assert any(
        "Portail started" in r.getMessage() for r in portail_records
    ), "Message text must be present in re-emitted record"
    # Without a correlation_id the message must NOT have a [xxxxxxxx] prefix
    for r in portail_records:
        msg = r.getMessage()
        if "Portail started" in msg:
            assert not msg.startswith("["), f"No [cid] prefix expected when no correlation_id: {msg!r}"
