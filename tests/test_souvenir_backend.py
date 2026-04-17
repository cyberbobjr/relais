"""Unit tests for atelier.souvenir_backend.SouvenirBackend.

All tests mock the synchronous Redis client so no real Redis instance is needed.
The _request() helper is tested indirectly through write/read/edit/ls/glob/grep calls.
"""

import json
import time
import uuid
import pytest
from unittest.mock import MagicMock, patch, call

from atelier.souvenir_backend import SouvenirBackend
from common.envelope_actions import (
    ACTION_MEMORY_FILE_LIST,
    ACTION_MEMORY_FILE_READ,
    ACTION_MEMORY_FILE_WRITE,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend(user_id: str = "user-1") -> tuple[SouvenirBackend, MagicMock]:
    """Return a SouvenirBackend with a mocked synchronous Redis client."""
    backend = SouvenirBackend(user_id=user_id)
    mock_redis = MagicMock()
    backend._redis = mock_redis
    return backend, mock_redis


def _response_msg(corr: str, **extra) -> tuple[bytes, dict[bytes, bytes]]:
    """Build a fake xread message matching a given correlation_id."""
    msg_id = f"{int(time.time() * 1000)}-0".encode()
    data = {"ok": True, "correlation_id": corr, **extra}
    return (msg_id, {b"payload": json.dumps(data).encode()})


def _mock_request(backend: SouvenirBackend, response_extra: dict) -> None:
    """Patch _request to return a canned response dict.

    Args:
        backend: SouvenirBackend instance to patch.
        response_extra: Dict merged with {"ok": True} as the response.
    """
    backend._request = MagicMock(return_value={"ok": True, **response_extra})


# ---------------------------------------------------------------------------
# write()
# ---------------------------------------------------------------------------


def test_write_success() -> None:
    """write() returns WriteResult with path set on success."""
    backend, _ = _make_backend()
    _mock_request(backend, {})

    result = backend.write("/memories/notes.md", "hello world")

    backend._request.assert_called_once_with(
        ACTION_MEMORY_FILE_WRITE,
        path="/memories/notes.md",
        content="hello world",
        overwrite=False,
    )
    assert result.path == "/memories/notes.md"
    assert result.error is None
    assert result.files_update is None


def test_write_returns_error_on_failure() -> None:
    """write() returns WriteResult with error set when the response ok=False."""
    backend, _ = _make_backend()
    backend._request = MagicMock(return_value={"ok": False, "error": "File already exists"})

    result = backend.write("/memories/notes.md", "content")

    assert result.error == "File already exists"
    assert result.path is None


# ---------------------------------------------------------------------------
# read()
# ---------------------------------------------------------------------------


def test_read_returns_numbered_lines() -> None:
    """read() formats file content with line numbers (cat -n style)."""
    backend, _ = _make_backend()
    backend._request = MagicMock(return_value={"ok": True, "content": "line1\nline2\nline3"})

    result = backend.read("/memories/notes.md")

    assert "1\tline1" in result
    assert "2\tline2" in result
    assert "3\tline3" in result


def test_read_respects_offset_and_limit() -> None:
    """read() slices lines according to offset and limit parameters."""
    backend, _ = _make_backend()
    content = "\n".join(f"line{i}" for i in range(1, 11))
    backend._request = MagicMock(return_value={"ok": True, "content": content})

    result = backend.read("/memories/notes.md", offset=2, limit=3)

    lines = result.strip().split("\n")
    assert len(lines) == 3
    assert "line3" in lines[0]  # offset=2 → 0-indexed line 2 = "line3"


def test_read_returns_error_string_on_failure() -> None:
    """read() returns an error string when the response ok=False."""
    backend, _ = _make_backend()
    backend._request = MagicMock(return_value={"ok": False, "error": "File not found"})

    result = backend.read("/memories/missing.md")

    assert "Error:" in result
    assert "File not found" in result


# ---------------------------------------------------------------------------
# edit()
# ---------------------------------------------------------------------------


def test_edit_success_single_occurrence() -> None:
    """edit() does read-modify-write and returns EditResult with occurrences=1."""
    backend, _ = _make_backend()
    call_count = {"n": 0}

    def fake_request(action, **kwargs):
        call_count["n"] += 1
        if action == ACTION_MEMORY_FILE_READ:
            return {"ok": True, "content": "hello world"}
        if action == ACTION_MEMORY_FILE_WRITE:
            return {"ok": True}
        return {"ok": False, "error": "unexpected"}

    backend._request = MagicMock(side_effect=fake_request)

    result = backend.edit("/memories/notes.md", "hello", "goodbye")

    assert result.error is None
    assert result.occurrences == 1
    assert result.files_update is None

    # Verify the written content
    write_call = [c for c in backend._request.call_args_list if c[0][0] == ACTION_MEMORY_FILE_WRITE][0]
    assert "goodbye world" in write_call[1]["content"]


def test_edit_returns_error_when_old_string_absent() -> None:
    """edit() returns EditResult with error when old_string is not in the file."""
    backend, _ = _make_backend()
    backend._request = MagicMock(return_value={"ok": True, "content": "some other content"})

    result = backend.edit("/memories/notes.md", "not present", "replacement")

    assert result.error is not None
    assert "not found" in result.error


def test_edit_returns_error_on_ambiguous_match() -> None:
    """edit() with replace_all=False returns error when old_string appears multiple times."""
    backend, _ = _make_backend()
    backend._request = MagicMock(return_value={"ok": True, "content": "foo foo foo"})

    result = backend.edit("/memories/notes.md", "foo", "bar", replace_all=False)

    assert result.error is not None
    assert "unique" in result.error or "occurrences" in result.error


def test_edit_replace_all_replaces_all_occurrences() -> None:
    """edit() with replace_all=True replaces every occurrence."""
    backend, _ = _make_backend()

    def fake_request(action, **kwargs):
        if action == ACTION_MEMORY_FILE_READ:
            return {"ok": True, "content": "cat cat cat"}
        return {"ok": True}

    backend._request = MagicMock(side_effect=fake_request)

    result = backend.edit("/memories/notes.md", "cat", "dog", replace_all=True)

    assert result.error is None
    assert result.occurrences == 3
    write_call = [c for c in backend._request.call_args_list if c[0][0] == ACTION_MEMORY_FILE_WRITE][0]
    assert write_call[1]["content"] == "dog dog dog"


# ---------------------------------------------------------------------------
# ls_info()
# ---------------------------------------------------------------------------


def test_ls_info_returns_file_info_list() -> None:
    """ls_info() maps the file_list response to a list of FileInfo objects."""
    backend, _ = _make_backend()
    backend._request = MagicMock(return_value={
        "ok": True,
        "files": [
            {"path": "/memories/a.md", "size": 10, "modified_at": "2026-04-03T10:00:00Z"},
            {"path": "/memories/b.md", "size": 20, "modified_at": "2026-04-03T11:00:00Z"},
        ],
    })

    result = backend.ls_info("/memories/")

    assert len(result) == 2
    assert result[0]["path"] == "/memories/a.md"
    assert result[0]["size"] == 10
    assert result[0]["is_dir"] is False


def test_ls_info_normalises_path_without_trailing_slash() -> None:
    """ls_info() appends a trailing slash if the path doesn't end with one."""
    backend, _ = _make_backend()
    backend._request = MagicMock(return_value={"ok": True, "files": []})

    backend.ls_info("/memories")

    backend._request.assert_called_once_with(ACTION_MEMORY_FILE_LIST, path="/memories/")


def test_ls_info_returns_empty_on_failure() -> None:
    """ls_info() returns an empty list when the response ok=False."""
    backend, _ = _make_backend()
    backend._request = MagicMock(return_value={"ok": False, "error": "something"})

    result = backend.ls_info("/memories/")

    assert result == []


# ---------------------------------------------------------------------------
# glob_info()
# ---------------------------------------------------------------------------


def test_glob_info_filters_by_pattern() -> None:
    """glob_info() returns only files matching the given glob pattern."""
    backend, _ = _make_backend()
    backend._request = MagicMock(return_value={
        "ok": True,
        "files": [
            {"path": "/memories/notes.md", "size": 5, "modified_at": "2026-04-03T10:00:00Z"},
            {"path": "/memories/data.json", "size": 8, "modified_at": "2026-04-03T10:00:00Z"},
        ],
    })

    result = backend.glob_info("*.md", "/memories/")

    assert len(result) == 1
    assert result[0]["path"] == "/memories/notes.md"


# ---------------------------------------------------------------------------
# grep_raw()
# ---------------------------------------------------------------------------


def test_grep_raw_finds_matching_lines() -> None:
    """grep_raw() returns GrepMatch entries for lines containing the pattern."""
    backend, _ = _make_backend()

    def fake_request(action, **kwargs):
        if action == ACTION_MEMORY_FILE_LIST:
            return {"ok": True, "files": [{"path": "/memories/notes.md", "size": 30, "modified_at": ""}]}
        if action == ACTION_MEMORY_FILE_READ:
            return {"ok": True, "content": "hello world\nfoo bar\nhello again"}
        return {"ok": False}

    backend._request = MagicMock(side_effect=fake_request)

    result = backend.grep_raw("hello", "/memories/")

    assert len(result) == 2
    assert result[0]["line"] == 1
    assert result[1]["line"] == 3
    assert "hello" in result[0]["text"]


def test_grep_raw_returns_empty_when_no_match() -> None:
    """grep_raw() returns an empty list when no line matches the pattern."""
    backend, _ = _make_backend()

    def fake_request(action, **kwargs):
        if action == ACTION_MEMORY_FILE_LIST:
            return {"ok": True, "files": [{"path": "/memories/notes.md", "size": 10, "modified_at": ""}]}
        if action == ACTION_MEMORY_FILE_READ:
            return {"ok": True, "content": "no match here\nor here"}
        return {"ok": False}

    backend._request = MagicMock(side_effect=fake_request)

    result = backend.grep_raw("missing_pattern", "/memories/")

    assert result == []


# ---------------------------------------------------------------------------
# upload_files() / download_files()
# ---------------------------------------------------------------------------


def test_upload_files_writes_each_file() -> None:
    """upload_files() calls file_write for each (path, bytes) pair."""
    backend, _ = _make_backend()
    backend._request = MagicMock(return_value={"ok": True})

    result = backend.upload_files([
        ("/memories/a.md", b"content a"),
        ("/memories/b.md", b"content b"),
    ])

    assert len(result) == 2
    assert all(r.error is None for r in result)
    assert backend._request.call_count == 2


def test_download_files_reads_each_file() -> None:
    """download_files() calls file_read for each path and returns decoded bytes."""
    backend, _ = _make_backend()
    backend._request = MagicMock(return_value={"ok": True, "content": "file content"})

    result = backend.download_files(["/memories/a.md", "/memories/b.md"])

    assert len(result) == 2
    assert all(r.error is None for r in result)
    assert result[0].content == b"file content"


def test_download_files_returns_error_on_failure() -> None:
    """download_files() marks individual files as error when file_read fails."""
    backend, _ = _make_backend()
    backend._request = MagicMock(return_value={"ok": False, "error": "File not found"})

    result = backend.download_files(["/memories/missing.md"])

    assert result[0].content is None
    assert result[0].error is not None


# ---------------------------------------------------------------------------
# _request() — timeout path
# ---------------------------------------------------------------------------


def test_request_returns_timeout_when_no_response() -> None:
    """_request() returns {'ok': False, 'error': 'timeout'} after the deadline passes."""
    backend, mock_redis = _make_backend()
    mock_redis.xrevrange.return_value = []
    mock_redis.xadd.return_value = None
    # xread always returns empty → simulates no matching response arriving
    mock_redis.xread.return_value = []

    with patch("atelier.souvenir_backend._TIMEOUT_S", 0.05):
        result = backend._request("file_read", path="/memories/notes.md")

    assert result == {"ok": False, "error": "timeout"}


def test_request_matches_by_correlation_id() -> None:
    """_request() ignores responses with a different correlation_id."""
    backend, mock_redis = _make_backend()
    mock_redis.xrevrange.return_value = []
    mock_redis.xadd.return_value = None

    other_corr = str(uuid.uuid4())
    # Return a response with a non-matching correlation_id, then nothing (indefinitely)
    other_response_payload = json.dumps({"ok": True, "correlation_id": other_corr}).encode()
    _responses = iter([
        [(b"relais:memory:response", [(b"1-0", {b"payload": other_response_payload})])],
    ])
    mock_redis.xread.side_effect = lambda *a, **kw: next(_responses, [])

    with patch("atelier.souvenir_backend._TIMEOUT_S", 0.05):
        result = backend._request("file_read", path="/memories/notes.md")

    # No match found for our corr → timeout
    assert result == {"ok": False, "error": "timeout"}
