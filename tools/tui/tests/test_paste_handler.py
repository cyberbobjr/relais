"""Tests for paste_handler module — Phase 1 TDD (RED → GREEN).

These tests must FAIL before the implementation files exist.
"""
from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# is_large_paste
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_is_large_paste_false() -> None:
    """4 newlines should NOT be considered a large paste (threshold=5)."""
    from relais_tui.paste_handler import is_large_paste

    text = "line1\nline2\nline3\nline4\nend"  # 4 newlines
    assert is_large_paste(text) is False


@pytest.mark.unit
def test_is_large_paste_true() -> None:
    """5 newlines SHOULD be considered a large paste (threshold=5)."""
    from relais_tui.paste_handler import is_large_paste

    text = "a\nb\nc\nd\ne\nf"  # 5 newlines
    assert is_large_paste(text) is True


@pytest.mark.unit
def test_is_large_paste_custom_threshold() -> None:
    """Custom threshold=3 with exactly 3 newlines should return True."""
    from relais_tui.paste_handler import is_large_paste

    text = "a\nb\nc\nd"  # 3 newlines
    assert is_large_paste(text, threshold=3) is True


@pytest.mark.unit
def test_is_large_paste_exactly_at_threshold() -> None:
    """Exactly threshold=5 newlines should return True (>= boundary)."""
    from relais_tui.paste_handler import is_large_paste

    text = "\n" * 5  # exactly 5 newlines
    assert is_large_paste(text) is True


@pytest.mark.unit
def test_is_large_paste_empty_string() -> None:
    """Empty string has 0 newlines — should return False."""
    from relais_tui.paste_handler import is_large_paste

    assert is_large_paste("") is False


# ---------------------------------------------------------------------------
# summarize_paste
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_summarize_paste_line_count() -> None:
    """A 10-line text produces a PasteBlock with line_count=10."""
    from relais_tui.paste_handler import summarize_paste

    text = "\n".join(f"line {i}" for i in range(10))
    block = summarize_paste(text)
    assert block.line_count == 10


@pytest.mark.unit
def test_summarize_paste_summary() -> None:
    """Summary property returns the canonical '[lines pasted: +N lines]' string."""
    from relais_tui.paste_handler import summarize_paste

    text = "\n".join(f"x{i}" for i in range(10))
    block = summarize_paste(text)
    assert block.summary == "[lines pasted: +10 lines]"


@pytest.mark.unit
def test_summarize_paste_preserves_full_text() -> None:
    """PasteBlock.full_text must equal the original text verbatim."""
    from relais_tui.paste_handler import summarize_paste

    text = "alpha\nbeta\ngamma\ndelta\nepsilon"
    block = summarize_paste(text)
    assert block.full_text == text


@pytest.mark.unit
def test_summarize_paste_single_line() -> None:
    """A single-line string produces line_count=1."""
    from relais_tui.paste_handler import summarize_paste

    block = summarize_paste("hello")
    assert block.line_count == 1


@pytest.mark.unit
def test_summarize_paste_is_frozen() -> None:
    """PasteBlock is a frozen dataclass — mutation raises TypeError."""
    from relais_tui.paste_handler import summarize_paste

    block = summarize_paste("a\nb\nc")
    with pytest.raises((AttributeError, TypeError)):
        block.line_count = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# detect_image_path
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_detect_image_path_none_for_multiline() -> None:
    """Multi-line text must not be detected as an image path."""
    from relais_tui.paste_handler import detect_image_path

    result = detect_image_path("/some/path.png\nextra line")
    assert result is None


@pytest.mark.unit
def test_detect_image_path_none_for_nonexistent() -> None:
    """A single-line path that doesn't exist on disk returns None."""
    from relais_tui.paste_handler import detect_image_path

    result = detect_image_path("/nonexistent/does_not_exist.png")
    assert result is None


@pytest.mark.unit
def test_detect_image_path_valid() -> None:
    """A valid single-line path pointing to an existing .png file returns the Path."""
    from relais_tui.paste_handler import detect_image_path

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG\r\n")  # minimal PNG header bytes
        tmp = Path(f.name)
    try:
        result = detect_image_path(str(tmp))
        assert result == tmp
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.unit
def test_detect_image_path_case_insensitive() -> None:
    """Upper-case extension (.PNG) should also be detected."""
    from relais_tui.paste_handler import detect_image_path

    with tempfile.NamedTemporaryFile(suffix=".PNG", delete=False) as f:
        f.write(b"\x89PNG\r\n")
        tmp = Path(f.name)
    try:
        result = detect_image_path(str(tmp))
        assert result == tmp
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.unit
def test_detect_image_path_non_image_extension() -> None:
    """A .txt file should not be detected as an image path."""
    from relais_tui.paste_handler import detect_image_path

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"hello")
        tmp = Path(f.name)
    try:
        result = detect_image_path(str(tmp))
        assert result is None
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.unit
def test_detect_image_path_jpeg() -> None:
    """JPEG extension should also be detected."""
    from relais_tui.paste_handler import detect_image_path

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(b"\xff\xd8\xff")  # minimal JPEG header
        tmp = Path(f.name)
    try:
        result = detect_image_path(str(tmp))
        assert result == tmp
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.unit
def test_detect_image_path_strips_whitespace() -> None:
    """Leading/trailing whitespace should be stripped before checking."""
    from relais_tui.paste_handler import detect_image_path

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG\r\n")
        tmp = Path(f.name)
    try:
        result = detect_image_path(f"  {tmp}  ")
        assert result == tmp
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# load_image_to_base64
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_load_image_to_base64() -> None:
    """Written bytes must survive a base64 round-trip in the payload."""
    from relais_tui.paste_handler import load_image_to_base64

    raw_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(raw_bytes)
        tmp = Path(f.name)
    try:
        payload = load_image_to_base64(tmp)
        assert payload.name == tmp.name
        assert payload.mime_type == "image/png"
        decoded = base64.b64decode(payload.data)
        assert decoded == raw_bytes
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.unit
def test_load_image_to_base64_jpeg() -> None:
    """JPEG files must be detected as image/jpeg MIME type."""
    from relais_tui.paste_handler import load_image_to_base64

    raw_bytes = b"\xff\xd8\xff\xe0"
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(raw_bytes)
        tmp = Path(f.name)
    try:
        payload = load_image_to_base64(tmp)
        assert "jpeg" in payload.mime_type or "jpg" in payload.mime_type
        decoded = base64.b64decode(payload.data)
        assert decoded == raw_bytes
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.unit
def test_load_image_to_base64_frozen() -> None:
    """ImagePayload is a frozen dataclass — mutation raises an error."""
    from relais_tui.paste_handler import load_image_to_base64
    from relais_tui.attachments import ImagePayload

    raw_bytes = b"\x89PNG"
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(raw_bytes)
        tmp = Path(f.name)
    try:
        payload = load_image_to_base64(tmp)
        with pytest.raises((AttributeError, TypeError)):
            payload.name = "other"  # type: ignore[misc]
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# attachments dataclasses
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_image_payload_frozen() -> None:
    """ImagePayload is frozen and cannot be mutated."""
    from relais_tui.attachments import ImagePayload

    payload = ImagePayload(name="test.png", mime_type="image/png", data="abc")
    with pytest.raises((AttributeError, TypeError)):
        payload.name = "other"  # type: ignore[misc]


@pytest.mark.unit
def test_paste_block_summary_property() -> None:
    """PasteBlock.summary returns correct format string."""
    from relais_tui.attachments import PasteBlock

    block = PasteBlock(full_text="a\nb\nc", line_count=3)
    assert block.summary == "[lines pasted: +3 lines]"


@pytest.mark.unit
def test_paste_block_frozen() -> None:
    """PasteBlock is frozen and cannot be mutated."""
    from relais_tui.attachments import PasteBlock

    block = PasteBlock(full_text="x", line_count=1)
    with pytest.raises((AttributeError, TypeError)):
        block.full_text = "y"  # type: ignore[misc]
