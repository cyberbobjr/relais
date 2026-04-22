"""Unit tests for ChunkPayload and decode_chunk in atelier.streaming — TDD RED first.

Tests validate:
- ChunkPayload is importable from atelier.streaming
- decode_chunk is importable from atelier.streaming
- decode_chunk returns None for non-dict input
- decode_chunk returns None when required keys are missing
- decode_chunk returns None when chunk is an empty dict
- decode_chunk returns a ChunkPayload for a valid updates chunk
- decode_chunk returns a ChunkPayload for a valid messages chunk
- ChunkPayload.source is 'agent' when ns is empty
- ChunkPayload.source is 'subagent:{ns[0]}' when ns is non-empty
- ChunkPayload fields are accessible by name
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chunk_payload_importable() -> None:
    """ChunkPayload must be importable from atelier.streaming."""
    from atelier.streaming import ChunkPayload  # noqa: F401


@pytest.mark.unit
def test_decode_chunk_importable() -> None:
    """decode_chunk must be importable from atelier.streaming."""
    from atelier.streaming import decode_chunk  # noqa: F401


# ---------------------------------------------------------------------------
# decode_chunk — invalid / unexpected input
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_decode_chunk_returns_none_for_non_dict() -> None:
    """decode_chunk must return None for non-dict input."""
    from atelier.streaming import decode_chunk

    assert decode_chunk("not a dict") is None
    assert decode_chunk(42) is None
    assert decode_chunk(None) is None
    assert decode_chunk([]) is None


@pytest.mark.unit
def test_decode_chunk_returns_none_for_empty_dict() -> None:
    """decode_chunk must return None for an empty dict."""
    from atelier.streaming import decode_chunk

    assert decode_chunk({}) is None


@pytest.mark.unit
def test_decode_chunk_returns_none_when_type_missing() -> None:
    """decode_chunk must return None when 'type' key is absent."""
    from atelier.streaming import decode_chunk

    assert decode_chunk({"ns": [], "data": {}}) is None


@pytest.mark.unit
def test_decode_chunk_returns_none_when_ns_missing() -> None:
    """decode_chunk must return None when 'ns' key is absent."""
    from atelier.streaming import decode_chunk

    assert decode_chunk({"type": "updates", "data": {}}) is None


@pytest.mark.unit
def test_decode_chunk_returns_none_when_data_missing() -> None:
    """decode_chunk must return None when 'data' key is absent."""
    from atelier.streaming import decode_chunk

    assert decode_chunk({"type": "updates", "ns": []}) is None


# ---------------------------------------------------------------------------
# decode_chunk — valid chunks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_decode_chunk_valid_updates_chunk() -> None:
    """decode_chunk returns a ChunkPayload for a well-formed updates chunk."""
    from atelier.streaming import decode_chunk

    raw = {"type": "updates", "ns": [], "data": {"model": {}}}
    result = decode_chunk(raw)
    assert result is not None
    assert result.chunk_type == "updates"
    assert result.ns == []
    assert result.data == {"model": {}}


@pytest.mark.unit
def test_decode_chunk_valid_messages_chunk() -> None:
    """decode_chunk returns a ChunkPayload for a well-formed messages chunk."""
    from atelier.streaming import decode_chunk

    token_mock = object()
    meta_mock = {}
    raw = {"type": "messages", "ns": ["ns1"], "data": (token_mock, meta_mock)}
    result = decode_chunk(raw)
    assert result is not None
    assert result.chunk_type == "messages"
    assert result.ns == ["ns1"]
    assert result.data == (token_mock, meta_mock)


# ---------------------------------------------------------------------------
# ChunkPayload.source
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chunk_payload_source_is_agent_when_ns_empty() -> None:
    """source must be 'agent' when ns is an empty list."""
    from atelier.streaming import decode_chunk

    raw = {"type": "updates", "ns": [], "data": {}}
    result = decode_chunk(raw)
    assert result is not None
    assert result.source == "agent"


@pytest.mark.unit
def test_chunk_payload_source_is_subagent_when_ns_non_empty() -> None:
    """source must be 'subagent:{ns[0]}' when ns is non-empty."""
    from atelier.streaming import decode_chunk

    raw = {"type": "updates", "ns": ["ns-abc"], "data": {}}
    result = decode_chunk(raw)
    assert result is not None
    assert result.source == "subagent:ns-abc"


# ---------------------------------------------------------------------------
# ChunkPayload field access
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chunk_payload_fields_accessible_by_name() -> None:
    """ChunkPayload fields chunk_type, ns, data, source must be accessible."""
    from atelier.streaming import ChunkPayload

    payload = ChunkPayload(chunk_type="updates", ns=["x"], data={"k": "v"})
    assert payload.chunk_type == "updates"
    assert payload.ns == ["x"]
    assert payload.data == {"k": "v"}
    assert payload.source == "subagent:x"
