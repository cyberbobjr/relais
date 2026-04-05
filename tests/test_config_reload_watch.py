"""Tests for common.config_reload — watch_and_reload() function.

TDD — tests are written before the implementation.  All tests are unit tests
that mock watchfiles and logging; no filesystem I/O or external dependency required.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# watch_and_reload — basic reload on change
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watch_and_reload_calls_reload_on_change() -> None:
    """watch_and_reload calls reload_fn once when awatch yields one change event."""
    from common.config_reload import watch_and_reload

    reload_called: list[bool] = []

    async def fake_reload() -> bool:
        reload_called.append(True)
        return True

    paths = [Path("/fake/config.yaml")]

    # awatch is an async generator — one batch of changes then stop
    async def fake_awatch(*args, **kwargs):
        yield {("/fake/config.yaml", 1)}  # one change event

    with patch("common.config_reload.watchfiles") as mock_wf:
        mock_wf.awatch = fake_awatch
        await watch_and_reload(paths, fake_reload, "test_label")

    assert reload_called == [True], "reload_fn must be called once for one change event"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watch_and_reload_logs_on_change(caplog) -> None:
    """watch_and_reload logs an info message containing the label on change."""
    from common.config_reload import watch_and_reload

    async def fake_reload() -> bool:
        return True

    paths = [Path("/fake/config.yaml")]

    async def fake_awatch(*args, **kwargs):
        yield {("/fake/config.yaml", 1)}

    with patch("common.config_reload.watchfiles") as mock_wf:
        mock_wf.awatch = fake_awatch
        with caplog.at_level(logging.INFO, logger="common.config_reload"):
            await watch_and_reload(paths, fake_reload, "portail")

    assert any("portail" in r.message for r in caplog.records), (
        "Expected label 'portail' in info log message"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watch_and_reload_logs_critical_on_reload_failure(caplog) -> None:
    """watch_and_reload logs CRITICAL when reload_fn returns False."""
    from common.config_reload import watch_and_reload

    async def failing_reload() -> bool:
        return False

    paths = [Path("/fake/config.yaml")]

    async def fake_awatch(*args, **kwargs):
        yield {("/fake/config.yaml", 1)}

    with patch("common.config_reload.watchfiles") as mock_wf:
        mock_wf.awatch = fake_awatch
        with caplog.at_level(logging.CRITICAL, logger="common.config_reload"):
            await watch_and_reload(paths, failing_reload, "atelier")

    assert any(r.levelno == logging.CRITICAL for r in caplog.records), (
        "Expected CRITICAL log when reload_fn returns False"
    )
    assert any("atelier" in r.message for r in caplog.records), (
        "Expected label 'atelier' in the critical log message"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watch_and_reload_continues_after_failure() -> None:
    """watch_and_reload calls reload_fn again after a failure on first change."""
    from common.config_reload import watch_and_reload

    call_count: list[bool] = []
    return_values = [False, True]

    async def flaky_reload() -> bool:
        call_count.append(True)
        return return_values[len(call_count) - 1]

    paths = [Path("/fake/config.yaml")]

    async def fake_awatch(*args, **kwargs):
        yield {("/fake/config.yaml", 1)}  # first change event
        yield {("/fake/config.yaml", 1)}  # second change event

    with patch("common.config_reload.watchfiles") as mock_wf:
        mock_wf.awatch = fake_awatch
        await watch_and_reload(paths, flaky_reload, "sentinelle")

    assert len(call_count) == 2, (
        "reload_fn must be called for each change event, even after a failure"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watch_and_reload_raises_on_missing_watchfiles() -> None:
    """watch_and_reload raises ImportError with helpful message when watchfiles is not installed."""
    from common.config_reload import watch_and_reload
    import common.config_reload as reload_mod

    # Patch the module-level watchfiles attribute to None (simulates not installed)
    with patch.object(reload_mod, "watchfiles", None):
        with pytest.raises(ImportError, match="watchfiles"):
            await watch_and_reload(
                [Path("/fake/config.yaml")],
                AsyncMock(return_value=True),
                "test",
            )
