"""Unit tests for souvenir.handlers file_write, file_read, file_list handlers."""

import json
import pytest
from unittest.mock import AsyncMock

from souvenir.handlers import HandlerContext
from souvenir.handlers.file_list_handler import FileListHandler
from souvenir.handlers.file_read_handler import FileReadHandler
from souvenir.handlers.file_write_handler import FileWriteHandler

pytestmark = pytest.mark.unit

_STREAM_RES = "relais:memory:response"


def _make_ctx(
    req: dict,
    file_store: AsyncMock | None = None,
    mock_redis: AsyncMock | None = None,
) -> HandlerContext:
    """Build a HandlerContext suitable for testing file handlers."""
    if mock_redis is None:
        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock()
    if file_store is None:
        file_store = AsyncMock()
    return HandlerContext(
        redis_conn=mock_redis,
        long_term_store=AsyncMock(),
        file_store=file_store,
        req=req,
        stream_res=_STREAM_RES,
    )


def _xadd_payload(mock_redis: AsyncMock) -> dict:
    """Extract the JSON payload from the last xadd call."""
    raw = mock_redis.xadd.call_args[0][1]["payload"]
    return json.loads(raw)


# ---------------------------------------------------------------------------
# FileWriteHandler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_write_handler_success() -> None:
    """FileWriteHandler publishes ok=True when FileStore.write_file returns None."""
    file_store = AsyncMock()
    file_store.write_file = AsyncMock(return_value=None)
    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()

    ctx = _make_ctx(
        req={
            "user_id": "user-1",
            "path": "/memories/notes.md",
            "content": "hello",
            "overwrite": False,
            "correlation_id": "c-1",
        },
        file_store=file_store,
        mock_redis=mock_redis,
    )

    await FileWriteHandler().handle(ctx)

    file_store.write_file.assert_awaited_once_with(
        user_id="user-1",
        path="/memories/notes.md",
        content="hello",
        overwrite=False,
    )
    payload = _xadd_payload(mock_redis)
    assert payload == {"correlation_id": "c-1", "ok": True, "error": None}


@pytest.mark.asyncio
async def test_file_write_handler_error_when_file_exists() -> None:
    """FileWriteHandler publishes ok=False when FileStore returns an error string."""
    file_store = AsyncMock()
    file_store.write_file = AsyncMock(return_value="File already exists: /memories/notes.md")
    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()

    ctx = _make_ctx(
        req={
            "user_id": "user-1",
            "path": "/memories/notes.md",
            "content": "hello",
            "correlation_id": "c-2",
        },
        file_store=file_store,
        mock_redis=mock_redis,
    )

    await FileWriteHandler().handle(ctx)

    payload = _xadd_payload(mock_redis)
    assert payload["ok"] is False
    assert "already exists" in payload["error"]
    assert payload["correlation_id"] == "c-2"


@pytest.mark.asyncio
async def test_file_write_handler_missing_user_id() -> None:
    """FileWriteHandler rejects requests missing user_id without calling FileStore."""
    file_store = AsyncMock()
    file_store.write_file = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()

    ctx = _make_ctx(
        req={"path": "/memories/notes.md", "content": "x", "correlation_id": "c-3"},
        file_store=file_store,
        mock_redis=mock_redis,
    )

    await FileWriteHandler().handle(ctx)

    file_store.write_file.assert_not_awaited()
    payload = _xadd_payload(mock_redis)
    assert payload["ok"] is False


@pytest.mark.asyncio
async def test_file_write_handler_overwrite_flag_forwarded() -> None:
    """FileWriteHandler passes overwrite=True to FileStore when set in request."""
    file_store = AsyncMock()
    file_store.write_file = AsyncMock(return_value=None)
    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()

    ctx = _make_ctx(
        req={
            "user_id": "user-1",
            "path": "/memories/notes.md",
            "content": "updated",
            "overwrite": True,
            "correlation_id": "c-4",
        },
        file_store=file_store,
        mock_redis=mock_redis,
    )

    await FileWriteHandler().handle(ctx)

    _, kwargs = file_store.write_file.call_args
    assert kwargs["overwrite"] is True


# ---------------------------------------------------------------------------
# FileReadHandler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_read_handler_success() -> None:
    """FileReadHandler publishes ok=True with content when file exists."""
    file_store = AsyncMock()
    file_store.read_file = AsyncMock(return_value=("file content", None))
    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()

    ctx = _make_ctx(
        req={
            "user_id": "user-1",
            "path": "/memories/notes.md",
            "correlation_id": "r-1",
        },
        file_store=file_store,
        mock_redis=mock_redis,
    )

    await FileReadHandler().handle(ctx)

    file_store.read_file.assert_awaited_once_with(user_id="user-1", path="/memories/notes.md")
    payload = _xadd_payload(mock_redis)
    assert payload == {
        "correlation_id": "r-1",
        "ok": True,
        "content": "file content",
        "error": None,
    }


@pytest.mark.asyncio
async def test_file_read_handler_not_found() -> None:
    """FileReadHandler publishes ok=False when FileStore returns an error."""
    file_store = AsyncMock()
    file_store.read_file = AsyncMock(return_value=(None, "File not found: /memories/notes.md"))
    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()

    ctx = _make_ctx(
        req={
            "user_id": "user-1",
            "path": "/memories/notes.md",
            "correlation_id": "r-2",
        },
        file_store=file_store,
        mock_redis=mock_redis,
    )

    await FileReadHandler().handle(ctx)

    payload = _xadd_payload(mock_redis)
    assert payload["ok"] is False
    assert payload["content"] is None
    assert "not found" in payload["error"].lower()


@pytest.mark.asyncio
async def test_file_read_handler_missing_user_id() -> None:
    """FileReadHandler rejects requests missing user_id without calling FileStore."""
    file_store = AsyncMock()
    file_store.read_file = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()

    ctx = _make_ctx(
        req={"path": "/memories/notes.md", "correlation_id": "r-3"},
        file_store=file_store,
        mock_redis=mock_redis,
    )

    await FileReadHandler().handle(ctx)

    file_store.read_file.assert_not_awaited()
    payload = _xadd_payload(mock_redis)
    assert payload["ok"] is False


# ---------------------------------------------------------------------------
# FileListHandler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_list_handler_success() -> None:
    """FileListHandler publishes ok=True with the files list."""
    sample_files = [
        {"path": "/memories/a.md", "size": 3, "modified_at": "2026-04-03T10:00:00Z"},
        {"path": "/memories/b.md", "size": 5, "modified_at": "2026-04-03T11:00:00Z"},
    ]
    file_store = AsyncMock()
    file_store.list_files = AsyncMock(return_value=sample_files)
    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()

    ctx = _make_ctx(
        req={
            "user_id": "user-1",
            "path": "/memories/",
            "correlation_id": "l-1",
        },
        file_store=file_store,
        mock_redis=mock_redis,
    )

    await FileListHandler().handle(ctx)

    file_store.list_files.assert_awaited_once_with(user_id="user-1", path_prefix="/memories/")
    payload = _xadd_payload(mock_redis)
    assert payload["ok"] is True
    assert payload["files"] == sample_files
    assert payload["error"] is None
    assert payload["correlation_id"] == "l-1"


@pytest.mark.asyncio
async def test_file_list_handler_empty_result() -> None:
    """FileListHandler publishes ok=True with an empty list when no files match."""
    file_store = AsyncMock()
    file_store.list_files = AsyncMock(return_value=[])
    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()

    ctx = _make_ctx(
        req={"user_id": "user-1", "path": "/memories/", "correlation_id": "l-2"},
        file_store=file_store,
        mock_redis=mock_redis,
    )

    await FileListHandler().handle(ctx)

    payload = _xadd_payload(mock_redis)
    assert payload["ok"] is True
    assert payload["files"] == []


@pytest.mark.asyncio
async def test_file_list_handler_uses_default_path() -> None:
    """FileListHandler defaults to '/memories/' when path is not in the request."""
    file_store = AsyncMock()
    file_store.list_files = AsyncMock(return_value=[])
    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()

    ctx = _make_ctx(
        req={"user_id": "user-1", "correlation_id": "l-3"},
        file_store=file_store,
        mock_redis=mock_redis,
    )

    await FileListHandler().handle(ctx)

    _, kwargs = file_store.list_files.call_args
    assert kwargs["path_prefix"] == "/memories/"


@pytest.mark.asyncio
async def test_file_list_handler_missing_user_id() -> None:
    """FileListHandler rejects requests missing user_id without calling FileStore."""
    file_store = AsyncMock()
    file_store.list_files = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()

    ctx = _make_ctx(
        req={"path": "/memories/", "correlation_id": "l-4"},
        file_store=file_store,
        mock_redis=mock_redis,
    )

    await FileListHandler().handle(ctx)

    file_store.list_files.assert_not_awaited()
    payload = _xadd_payload(mock_redis)
    assert payload["ok"] is False
