"""Tests verifying shutdown is wired into every brick's main loop.

Each test simulates a shutdown signal by using a pre-set asyncio.Event
and asserts that the brick's _run_stream_loop exits without hanging.

The pattern used for every brick:
  - Build a pre-set asyncio.Event (shutdown_event.set() before passing).
  - Provide a minimal async Redis mock that returns empty results.
  - Call _run_stream_loop(spec, redis_conn, shutdown_event) and assert it
    returns within a tight timeout.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_redis_mock(xreadgroup_side_effect=None):
    """Build a minimal async Redis mock suitable for all bricks.

    Args:
        xreadgroup_side_effect: Optional side_effect list for xreadgroup calls.
            Defaults to a single empty-result.

    Returns:
        MagicMock with async methods for xgroup_create, xreadgroup, xack, xadd.
    """
    redis_conn = MagicMock()
    redis_conn.xgroup_create = AsyncMock(return_value="OK")
    redis_conn.xack = AsyncMock(return_value=1)
    redis_conn.xadd = AsyncMock(return_value=b"1-0")
    redis_conn.xread = AsyncMock(return_value=[])

    if xreadgroup_side_effect is not None:
        redis_conn.xreadgroup = AsyncMock(side_effect=xreadgroup_side_effect)
    else:
        redis_conn.xreadgroup = AsyncMock(return_value=[])

    return redis_conn


# ---------------------------------------------------------------------------
# T1: Portail exits when shutdown is requested
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_exits_on_shutdown() -> None:
    """Portail._run_stream_loop exits when shutdown_event is already set.

    Wires a pre-set asyncio.Event and asserts the coroutine completes within
    1 second (no infinite loop).
    """
    from portail.main import Portail

    portail = Portail.__new__(Portail)
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"

    redis_conn = _make_redis_mock()

    shutdown_event = asyncio.Event()
    shutdown_event.set()

    spec = portail.stream_specs()[0]

    try:
        await asyncio.wait_for(
            portail._run_stream_loop(spec, redis_conn, shutdown_event),
            timeout=1.0,
        )
    except asyncio.TimeoutError:
        pytest.fail(
            "Portail._run_stream_loop did not exit after shutdown was requested "
            "(loop still running after 1 s — shutdown_event not wired)"
        )


# ---------------------------------------------------------------------------
# T2: Sentinelle exits when shutdown is requested
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinelle_exits_on_shutdown() -> None:
    """Sentinelle._run_stream_loop exits when shutdown_event is already set.

    Wires a pre-set asyncio.Event and asserts the coroutine completes within
    1 second (no infinite loop).
    """
    from sentinelle.main import Sentinelle

    sentinelle = Sentinelle.__new__(Sentinelle)
    sentinelle.stream_in = "relais:security"
    sentinelle.stream_out = "relais:tasks"
    sentinelle.group_name = "sentinelle_group"
    sentinelle.consumer_name = "sentinelle_1"

    redis_conn = _make_redis_mock()

    shutdown_event = asyncio.Event()
    shutdown_event.set()

    spec = sentinelle.stream_specs()[0]

    try:
        await asyncio.wait_for(
            sentinelle._run_stream_loop(spec, redis_conn, shutdown_event),
            timeout=1.0,
        )
    except asyncio.TimeoutError:
        pytest.fail(
            "Sentinelle._run_stream_loop did not exit after shutdown was requested "
            "(loop still running after 1 s — shutdown_event not wired)"
        )


# ---------------------------------------------------------------------------
# T3: Souvenir (request stream) exits when shutdown is requested
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_souvenir_request_stream_exits_on_shutdown() -> None:
    """Souvenir._run_stream_loop exits when shutdown_event is already set.

    Wires a pre-set asyncio.Event and asserts the coroutine completes within
    1 second (no infinite loop).
    """
    from souvenir.main import Souvenir

    souvenir = Souvenir.__new__(Souvenir)
    souvenir.stream_req = "relais:memory:request"
    souvenir.stream_res = "relais:memory:response"
    souvenir.group_name = "souvenir_group"
    souvenir.consumer_name = "souvenir_1"

    redis_conn = _make_redis_mock()

    shutdown_event = asyncio.Event()
    shutdown_event.set()

    spec = souvenir.stream_specs()[0]

    try:
        await asyncio.wait_for(
            souvenir._run_stream_loop(spec, redis_conn, shutdown_event),
            timeout=1.0,
        )
    except asyncio.TimeoutError:
        pytest.fail(
            "Souvenir._run_stream_loop did not exit after shutdown was requested "
            "(loop still running after 1 s — shutdown_event not wired)"
        )


# ---------------------------------------------------------------------------
# T4: Archiviste exits when shutdown is requested
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_archiviste_exits_on_shutdown() -> None:
    """Archiviste._process_stream exits when shutdown_event is set.

    Passes a pre-set asyncio.Event and asserts the coroutine completes within
    1 second (no infinite loop).
    """
    from archiviste.main import Archiviste

    archiviste = Archiviste.__new__(Archiviste)
    archiviste.base_dir = MagicMock()
    archiviste.events_log = MagicMock()
    archiviste.system_log = MagicMock()

    redis_conn = _make_redis_mock()

    shutdown_event = asyncio.Event()
    shutdown_event.set()

    # _write_event uses open() — patch it so no filesystem access occurs
    with patch.object(archiviste, "_write_event", return_value=None):
        try:
            await asyncio.wait_for(
                archiviste._process_stream(redis_conn, shutdown_event),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            pytest.fail(
                "Archiviste._process_stream did not exit after shutdown was requested "
                "(loop still running after 1 s — shutdown_event not checked)"
            )


# ---------------------------------------------------------------------------
# T5: Atelier exits when shutdown is requested
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_exits_on_shutdown() -> None:
    """Atelier._run_stream_loop exits when shutdown_event is already set.

    Wires a pre-set asyncio.Event and asserts the coroutine completes within
    1 second (no infinite loop).
    """
    from atelier.main import Atelier

    atelier = Atelier.__new__(Atelier)
    atelier.stream_in = "relais:tasks"
    atelier.group_name = "atelier_group"
    atelier.consumer_name = "atelier_1"

    redis_conn = _make_redis_mock()

    shutdown_event = asyncio.Event()
    shutdown_event.set()

    spec = atelier.stream_specs()[0]

    try:
        await asyncio.wait_for(
            atelier._run_stream_loop(spec, redis_conn, shutdown_event),
            timeout=1.0,
        )
    except asyncio.TimeoutError:
        pytest.fail(
            "Atelier._run_stream_loop did not exit after shutdown was requested "
            "(loop still running after 1 s — shutdown_event not wired)"
        )


# ---------------------------------------------------------------------------
# T6: install_signal_handlers is called in start() for each brick
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_calls_install_signal_handlers() -> None:
    """Portail.start() calls shutdown.install_signal_handlers() before the loop.

    Verifies that signal handler registration happens at startup, not only
    on the first message.
    """
    from portail.main import Portail

    portail = Portail.__new__(Portail)
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"

    mock_client = MagicMock()
    redis_conn = _make_redis_mock()
    mock_client.get_connection = AsyncMock(return_value=redis_conn)
    mock_client.close = AsyncMock()
    portail.client = mock_client

    install_called = []

    class _TrackingShutdown:
        def __init__(self):
            self._stop_event = asyncio.Event()
            self._stop_event.set()

        def install_signal_handlers(self):
            install_called.append(True)

        def is_stopping(self):
            return self._stop_event.is_set()

        @property
        def stop_event(self):
            return self._stop_event

        def register(self, task):
            pass

        async def wait_for_tasks(self, timeout=None):
            pass

    with patch("common.brick_base.GracefulShutdown", return_value=_TrackingShutdown()):
        await portail.start()

    assert install_called, (
        "Portail.start() did not call shutdown.install_signal_handlers() — "
        "signal handlers will not be registered at process startup"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinelle_calls_install_signal_handlers() -> None:
    """Sentinelle.start() calls shutdown.install_signal_handlers() before the loop.

    Verifies that signal handler registration happens at startup.
    """
    from sentinelle.main import Sentinelle

    sentinelle = Sentinelle.__new__(Sentinelle)
    sentinelle.stream_in = "relais:security"
    sentinelle.stream_out = "relais:tasks"
    sentinelle.group_name = "sentinelle_group"
    sentinelle.consumer_name = "sentinelle_1"

    mock_client = MagicMock()
    redis_conn = _make_redis_mock()
    mock_client.get_connection = AsyncMock(return_value=redis_conn)
    mock_client.close = AsyncMock()
    sentinelle.client = mock_client

    install_called = []

    class _TrackingShutdown:
        def __init__(self):
            self._stop_event = asyncio.Event()
            self._stop_event.set()

        def install_signal_handlers(self):
            install_called.append(True)

        def is_stopping(self):
            return self._stop_event.is_set()

        @property
        def stop_event(self):
            return self._stop_event

        def register(self, task):
            pass

        async def wait_for_tasks(self, timeout=None):
            pass

    with patch("common.brick_base.GracefulShutdown", return_value=_TrackingShutdown()):
        await sentinelle.start()

    assert install_called, (
        "Sentinelle.start() did not call shutdown.install_signal_handlers()"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_calls_install_signal_handlers() -> None:
    """Atelier.start() calls shutdown.install_signal_handlers() before the loop.

    Verifies that signal handler registration happens at startup.
    """
    from atelier.main import Atelier

    atelier = Atelier.__new__(Atelier)
    atelier.stream_in = "relais:tasks"
    atelier.group_name = "atelier_group"
    atelier.consumer_name = "atelier_1"
    mock_checkpointer = MagicMock()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_checkpointer)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    atelier._checkpointer_cm = mock_cm
    atelier._checkpointer = None

    mock_client = MagicMock()
    redis_conn = _make_redis_mock()
    mock_client.get_connection = AsyncMock(return_value=redis_conn)
    mock_client.close = AsyncMock()
    atelier.client = mock_client

    install_called = []

    class _TrackingShutdown:
        def __init__(self):
            self._stop_event = asyncio.Event()
            self._stop_event.set()

        def install_signal_handlers(self):
            install_called.append(True)

        def is_stopping(self):
            return self._stop_event.is_set()

        @property
        def stop_event(self):
            return self._stop_event

        def register(self, task):
            pass

        async def wait_for_tasks(self, timeout=None):
            pass

    with patch("common.brick_base.GracefulShutdown", return_value=_TrackingShutdown()):
        await atelier.start()

    assert install_called, (
        "Atelier.start() did not call shutdown.install_signal_handlers()"
    )
