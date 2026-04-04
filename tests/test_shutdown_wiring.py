"""Tests verifying GracefulShutdown is wired into every brick's main loop.

Each test simulates a shutdown signal by pre-setting GracefulShutdown.stop_event
(or replacing is_stopping with a one-shot sentinel) and asserts that the brick's
_process_stream (or equivalent) exits without hanging.

The pattern used for every brick:
  - Patch ``common.shutdown.GracefulShutdown`` so tests control the stop_event.
  - Provide a minimal async Redis mock that returns empty results.
  - Call the process method and assert it returns within a tight timeout.
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
            Defaults to a single empty-result followed by StopAsyncIteration to
            break the loop (but the loop should break on shutdown first).

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


class _PreSetShutdown:
    """Stub GracefulShutdown whose stop_event is already set at construction time.

    This causes any ``while not shutdown.is_stopping()`` loop to exit immediately
    on first evaluation, making tests deterministic without needing real signals.
    """

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._stop_event.set()  # already triggered

    def install_signal_handlers(self) -> None:
        """No-op: signals are not relevant in unit test context."""

    def is_stopping(self) -> bool:
        """Returns True always (shutdown pre-triggered).

        Returns:
            True.
        """
        return self._stop_event.is_set()

    @property
    def stop_event(self) -> asyncio.Event:
        """Returns the pre-set stop event.

        Returns:
            An asyncio.Event that is already set.
        """
        return self._stop_event

    def register(self, task) -> None:
        """No-op task registration.

        Args:
            task: Ignored.
        """

    async def wait_for_tasks(self, timeout=None) -> None:
        """No-op wait.

        Args:
            timeout: Ignored.
        """


# ---------------------------------------------------------------------------
# T1: Portail exits when shutdown is requested
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_portail_exits_on_shutdown() -> None:
    """Portail._process_stream exits when GracefulShutdown.is_stopping() is True.

    Wires a pre-set shutdown stub and asserts the coroutine completes within
    1 second (no infinite loop).
    """
    from portail.main import Portail

    portail = Portail.__new__(Portail)
    portail.stream_in = "relais:messages:incoming"
    portail.stream_out = "relais:security"
    portail.group_name = "portail_group"
    portail.consumer_name = "portail_1"

    redis_conn = _make_redis_mock()

    with patch("portail.main.GracefulShutdown", return_value=_PreSetShutdown()):
        try:
            await asyncio.wait_for(
                portail._process_stream(redis_conn),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            pytest.fail(
                "Portail._process_stream did not exit after shutdown was requested "
                "(loop still running after 1 s — GracefulShutdown not wired)"
            )


# ---------------------------------------------------------------------------
# T2: Sentinelle exits when shutdown is requested
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sentinelle_exits_on_shutdown() -> None:
    """Sentinelle._process_stream exits when GracefulShutdown.is_stopping() is True.

    Wires a pre-set shutdown stub and asserts the coroutine completes within
    1 second (no infinite loop).
    """
    from sentinelle.main import Sentinelle

    sentinelle = Sentinelle.__new__(Sentinelle)
    sentinelle.stream_in = "relais:security"
    sentinelle.stream_out = "relais:tasks"
    sentinelle.group_name = "sentinelle_group"
    sentinelle.consumer_name = "sentinelle_1"

    redis_conn = _make_redis_mock()

    with patch("sentinelle.main.GracefulShutdown", return_value=_PreSetShutdown()):
        try:
            await asyncio.wait_for(
                sentinelle._process_stream(redis_conn),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            pytest.fail(
                "Sentinelle._process_stream did not exit after shutdown was requested "
                "(loop still running after 1 s — GracefulShutdown not wired)"
            )


# ---------------------------------------------------------------------------
# T3: Souvenir (request stream) exits when shutdown is requested
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_souvenir_request_stream_exits_on_shutdown() -> None:
    """Souvenir._process_request_stream exits when GracefulShutdown.is_stopping() is True.

    Wires a pre-set shutdown stub and asserts the coroutine completes within
    1 second (no infinite loop).
    """
    from souvenir.main import Souvenir

    souvenir = Souvenir.__new__(Souvenir)
    souvenir.stream_req = "relais:memory:request"
    souvenir.stream_res = "relais:memory:response"
    souvenir.group_name = "souvenir_group"
    souvenir.consumer_name = "souvenir_1"

    redis_conn = _make_redis_mock()

    with patch("souvenir.main.GracefulShutdown", return_value=_PreSetShutdown()):
        try:
            await asyncio.wait_for(
                souvenir._process_request_stream(redis_conn),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            pytest.fail(
                "Souvenir._process_request_stream did not exit after shutdown was requested "
                "(loop still running after 1 s — GracefulShutdown not wired)"
            )


# ---------------------------------------------------------------------------
# T3b: Souvenir (outgoing stream) exits when shutdown is requested
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T4: Archiviste exits when shutdown is requested
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_archiviste_exits_on_shutdown() -> None:
    """Archiviste._process_stream exits when GracefulShutdown.is_stopping() is True.

    Wires a pre-set shutdown stub and asserts the coroutine completes within
    1 second (no infinite loop).
    """
    from archiviste.main import Archiviste

    archiviste = Archiviste.__new__(Archiviste)
    archiviste.base_dir = MagicMock()
    archiviste.events_log = MagicMock()
    archiviste.system_log = MagicMock()

    redis_conn = _make_redis_mock()

    with patch("archiviste.main.GracefulShutdown", return_value=_PreSetShutdown()):
        # _write_event uses open() — patch it so no filesystem access occurs
        with patch.object(archiviste, "_write_event", return_value=None):
            try:
                await asyncio.wait_for(
                    archiviste._process_stream(redis_conn),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                pytest.fail(
                    "Archiviste._process_stream did not exit after shutdown was requested "
                    "(loop still running after 1 s — GracefulShutdown not wired)"
                )


# ---------------------------------------------------------------------------
# T5: Atelier exits when shutdown is requested
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atelier_exits_on_shutdown() -> None:
    """Atelier._process_stream exits when GracefulShutdown.is_stopping() is True.

    Wires a pre-set shutdown stub and asserts the coroutine completes within
    1 second (no infinite loop).
    """
    from atelier.main import Atelier

    atelier = Atelier.__new__(Atelier)
    atelier.stream_in = "relais:tasks"
    atelier.group_name = "atelier_group"
    atelier.consumer_name = "atelier_1"

    redis_conn = _make_redis_mock()

    with patch("atelier.main.GracefulShutdown", return_value=_PreSetShutdown()):
        try:
            await asyncio.wait_for(
                atelier._process_stream(redis_conn),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            pytest.fail(
                "Atelier._process_stream did not exit after shutdown was requested "
                "(loop still running after 1 s — GracefulShutdown not wired)"
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

    stub = _PreSetShutdown()
    install_called = []

    original_install = stub.install_signal_handlers

    def _tracking_install():
        install_called.append(True)
        original_install()

    stub.install_signal_handlers = _tracking_install

    with patch("portail.main.GracefulShutdown", return_value=stub):
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

    stub = _PreSetShutdown()
    install_called = []
    original_install = stub.install_signal_handlers

    def _tracking_install():
        install_called.append(True)
        original_install()

    stub.install_signal_handlers = _tracking_install

    with patch("sentinelle.main.GracefulShutdown", return_value=stub):
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

    stub = _PreSetShutdown()
    install_called = []
    original_install = stub.install_signal_handlers

    def _tracking_install():
        install_called.append(True)
        original_install()

    stub.install_signal_handlers = _tracking_install

    with patch("atelier.main.GracefulShutdown", return_value=stub):
        await atelier.start()

    assert install_called, (
        "Atelier.start() did not call shutdown.install_signal_handlers()"
    )
