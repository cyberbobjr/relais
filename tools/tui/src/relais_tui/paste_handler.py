"""Paste detection and processing utilities for the ChatInput widget.

Handles three categories of paste events:
- **Large text**: compacted into a ``PasteBlock`` summary for display.
- **Image paths**: single-line path pointing to a supported image file.
- **Clipboard images**: grabbed directly from the system clipboard (macOS /
  PIL fallback).
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from relais_tui.attachments import ImagePayload, PasteBlock

IMAGE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
LARGE_PASTE_THRESHOLD: int = 5  # number of newlines that triggers compaction


def is_large_paste(text: str, threshold: int = LARGE_PASTE_THRESHOLD) -> bool:
    """Return True if *text* contains at least *threshold* newline characters.

    This mirrors Claude Code's behaviour: pastes at or above the threshold are
    compacted into a summary block rather than inserted verbatim.

    Args:
        text: The pasted string to inspect.
        threshold: Minimum number of newlines required for the paste to be
            considered "large". Defaults to ``LARGE_PASTE_THRESHOLD`` (5).

    Returns:
        ``True`` when ``text.count("\\n") >= threshold``, ``False`` otherwise.
    """
    return text.count("\n") >= threshold


def summarize_paste(text: str) -> PasteBlock:
    """Create a ``PasteBlock`` for a large paste, preserving the full content.

    The returned block stores the original text verbatim in ``full_text`` and
    exposes a short ``summary`` string for display inside the input widget.

    Args:
        text: The full pasted string.

    Returns:
        A frozen ``PasteBlock`` with ``line_count`` set to the number of
        physical lines (``len(text.splitlines())``).
    """
    lines = text.splitlines()
    return PasteBlock(full_text=text, line_count=len(lines))


def detect_image_path(text: str) -> Path | None:
    """Return a ``Path`` if *text* is a single line pointing to an image file.

    The function strips leading/trailing whitespace and rejects any input that
    contains a newline (multi-line pastes cannot be a single path).  The
    extension check is case-insensitive.  The file must exist on disk.

    Args:
        text: Candidate string (typically a clipboard paste).

    Returns:
        A resolved ``Path`` when *text* is a valid, existing image path;
        ``None`` in all other cases.
    """
    stripped = text.strip()
    if "\n" in stripped:
        return None
    p = Path(stripped).expanduser()
    if p.suffix.lower() in IMAGE_EXTENSIONS and p.exists():
        return p
    return None


def load_image_to_base64(path: Path) -> ImagePayload:
    """Read an image file from disk and return an ``ImagePayload``.

    The MIME type is guessed from the file extension via ``mimetypes``; if the
    type cannot be determined it falls back to ``"image/png"``.

    Args:
        path: Absolute or relative path to the image file.

    Returns:
        A frozen ``ImagePayload`` with ``name`` set to ``path.name``,
        ``mime_type`` from ``mimetypes``, and ``data`` as ASCII base64.

    Raises:
        FileNotFoundError: If *path* does not exist.
        OSError: If the file cannot be read.
    """
    mime, _ = mimetypes.guess_type(str(path))
    if mime is None:
        mime = "image/png"
    raw = path.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    return ImagePayload(name=path.name, mime_type=mime, data=encoded)


def grab_image_from_clipboard() -> ImagePayload | None:
    """Try to grab an image from the system clipboard.

    Attempts macOS ``osascript`` first, then falls back to ``PIL.ImageGrab``.
    Returns ``None`` if no image is available or on any error.

    Returns:
        An ``ImagePayload`` with ``name="clipboard"`` on success, or ``None``
        when no image could be retrieved.
    """
    import sys

    # macOS path — write clipboard PNG to a temp file via osascript
    if sys.platform == "darwin":
        import subprocess
        import tempfile

        tmp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                tmp = Path(f.name)
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    (
                        f'tell application "System Events" to write '
                        f'(the clipboard as «class PNGf») to '
                        f'(open for access POSIX file "{tmp}" with write permission)'
                    ),
                ],
                capture_output=True,
                timeout=3,
            )
            if result.returncode == 0 and tmp.stat().st_size > 0:
                return load_image_to_base64(tmp)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    # Cross-platform fallback via Pillow
    try:
        import io

        from PIL import ImageGrab

        img = ImageGrab.grabclipboard()
        if img is not None:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            encoded = base64.b64encode(buf.getvalue()).decode("ascii")
            return ImagePayload(name="clipboard", mime_type="image/png", data=encoded)
    except ImportError:
        pass

    return None
