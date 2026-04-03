"""Unit tests for souvenir.file_store.FileStore — CRUD operations on memory_files."""

import pytest
import pytest_asyncio

from souvenir.file_store import FileStore

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture
async def store(tmp_path):
    """Provide an isolated FileStore backed by a temporary SQLite database."""
    s = FileStore(db_path=tmp_path / "test_memory.db")
    await s._create_tables()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# write_file — create-only semantics (overwrite=False)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_creates_new_file(store):
    """write_file returns None (success) when the file does not exist yet."""
    error = await store.write_file("user-1", "/memories/notes.md", "hello world")
    assert error is None


@pytest.mark.asyncio
async def test_write_returns_error_when_file_exists(store):
    """write_file with overwrite=False returns an error string when the file already exists."""
    await store.write_file("user-1", "/memories/notes.md", "first")
    error = await store.write_file("user-1", "/memories/notes.md", "second")
    assert error is not None
    assert "already exists" in error


@pytest.mark.asyncio
async def test_write_different_users_same_path_independent(store):
    """Two users can own files at the same path without conflict."""
    err1 = await store.write_file("user-a", "/memories/notes.md", "a content")
    err2 = await store.write_file("user-b", "/memories/notes.md", "b content")
    assert err1 is None
    assert err2 is None


# ---------------------------------------------------------------------------
# write_file — upsert semantics (overwrite=True)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_overwrite_creates_when_absent(store):
    """write_file with overwrite=True creates the file when it doesn't exist."""
    error = await store.write_file("user-1", "/memories/notes.md", "created", overwrite=True)
    assert error is None


@pytest.mark.asyncio
async def test_write_overwrite_replaces_existing_content(store):
    """write_file with overwrite=True replaces the content of an existing file."""
    await store.write_file("user-1", "/memories/notes.md", "old content")
    await store.write_file("user-1", "/memories/notes.md", "new content", overwrite=True)
    content, error = await store.read_file("user-1", "/memories/notes.md")
    assert error is None
    assert content == "new content"


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_returns_content_for_existing_file(store):
    """read_file returns (content, None) for an existing file."""
    await store.write_file("user-1", "/memories/notes.md", "some content")
    content, error = await store.read_file("user-1", "/memories/notes.md")
    assert error is None
    assert content == "some content"


@pytest.mark.asyncio
async def test_read_returns_error_for_missing_file(store):
    """read_file returns (None, error_string) when the file does not exist."""
    content, error = await store.read_file("user-1", "/memories/missing.md")
    assert content is None
    assert error is not None
    assert "not found" in error.lower() or "File not found" in error


@pytest.mark.asyncio
async def test_read_is_user_scoped(store):
    """read_file for user-b cannot see user-a's file at the same path."""
    await store.write_file("user-a", "/memories/notes.md", "user-a content")
    content, error = await store.read_file("user-b", "/memories/notes.md")
    assert content is None
    assert error is not None


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_files_under_prefix(store):
    """list_files returns all files whose path starts with the given prefix."""
    await store.write_file("user-1", "/memories/a.md", "aaa")
    await store.write_file("user-1", "/memories/b.md", "bbb")
    files = await store.list_files("user-1", "/memories/")
    paths = {f["path"] for f in files}
    assert paths == {"/memories/a.md", "/memories/b.md"}


@pytest.mark.asyncio
async def test_list_excludes_other_users(store):
    """list_files returns only the requesting user's files."""
    await store.write_file("user-a", "/memories/shared.md", "a")
    await store.write_file("user-b", "/memories/shared.md", "b")
    files = await store.list_files("user-a", "/memories/")
    assert len(files) == 1
    assert files[0]["path"] == "/memories/shared.md"


@pytest.mark.asyncio
async def test_list_includes_size_and_modified_at(store):
    """list_files entries include 'size' (int bytes) and 'modified_at' (ISO 8601 string)."""
    content = "hello"
    await store.write_file("user-1", "/memories/hello.md", content)
    files = await store.list_files("user-1", "/memories/")
    assert len(files) == 1
    f = files[0]
    assert f["size"] == len(content.encode("utf-8"))
    assert f["modified_at"].endswith("Z")


@pytest.mark.asyncio
async def test_list_empty_when_no_files(store):
    """list_files returns an empty list when no files match the prefix."""
    files = await store.list_files("user-1", "/memories/")
    assert files == []


@pytest.mark.asyncio
async def test_list_ordered_by_path(store):
    """list_files returns files sorted ascending by path."""
    await store.write_file("user-1", "/memories/z.md", "z")
    await store.write_file("user-1", "/memories/a.md", "a")
    await store.write_file("user-1", "/memories/m.md", "m")
    files = await store.list_files("user-1", "/memories/")
    paths = [f["path"] for f in files]
    assert paths == sorted(paths)
