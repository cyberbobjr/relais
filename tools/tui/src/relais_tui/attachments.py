"""Attachment dataclasses for inline images and large paste blocks.

These frozen dataclasses carry attachment metadata through the TUI pipeline
without mutation, ensuring safe concurrent access.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ImagePayload:
    """Inline image attachment ready to send as base64.

    Args:
        name: Filename or ``"clipboard"`` when grabbed from the system clipboard.
        mime_type: MIME type such as ``"image/png"`` or ``"image/jpeg"``.
        data: Base64-encoded image bytes (no data-URI prefix).
    """

    name: str
    mime_type: str
    data: str


@dataclass(frozen=True)
class PasteBlock:
    """Large multi-line paste compacted for display in the input widget.

    The full text is preserved so it can be recovered when the user submits
    the message, while only the summary is shown in the input area.

    Args:
        full_text: The complete pasted content, preserved verbatim.
        line_count: Number of lines in ``full_text`` (used in the summary).
    """

    full_text: str
    line_count: int

    @property
    def summary(self) -> str:
        """Return a short human-readable summary shown inside the input widget.

        Returns:
            A string of the form ``"[lines pasted: +N lines]"``.
        """
        return f"[lines pasted: +{self.line_count} lines]"
