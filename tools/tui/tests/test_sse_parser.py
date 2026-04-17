"""Tests for relais_tui.sse_parser — TDD RED phase."""

from __future__ import annotations

import json

import pytest

from relais_tui.sse_parser import (
    DoneEvent,
    ErrorEvent,
    Keepalive,
    ProgressEvent,
    SSEParser,
    TokenEvent,
)


class TestTokenEvent:
    """TokenEvent dataclass tests."""

    def test_fields(self) -> None:
        ev = TokenEvent(text="hello")
        assert ev.text == "hello"

    def test_frozen(self) -> None:
        ev = TokenEvent(text="x")
        with pytest.raises(AttributeError):
            ev.text = "y"  # type: ignore[misc]


class TestDoneEvent:
    """DoneEvent dataclass tests."""

    def test_fields(self) -> None:
        ev = DoneEvent(content="full reply", correlation_id="abc", session_id="s1")
        assert ev.content == "full reply"
        assert ev.correlation_id == "abc"
        assert ev.session_id == "s1"


class TestProgressEvent:
    """ProgressEvent dataclass tests."""

    def test_fields(self) -> None:
        ev = ProgressEvent(event="tool_call", detail="web_search")
        assert ev.event == "tool_call"
        assert ev.detail == "web_search"


class TestErrorEvent:
    """ErrorEvent dataclass tests."""

    def test_fields(self) -> None:
        ev = ErrorEvent(error="timeout", correlation_id="abc")
        assert ev.error == "timeout"
        assert ev.correlation_id == "abc"


class TestKeepalive:
    """Keepalive sentinel tests."""

    def test_singleton(self) -> None:
        a = Keepalive()
        b = Keepalive()
        assert a == b

    def test_repr(self) -> None:
        assert "Keepalive" in repr(Keepalive())


# ---------------------------------------------------------------------------
# SSEParser — complete events
# ---------------------------------------------------------------------------


class TestSSEParserCompleteEvents:
    """Feed complete SSE frames and verify emitted events."""

    def test_token_event(self) -> None:
        parser = SSEParser()
        frame = b'event: token\ndata: {"t": "Hello"}\n\n'
        events = list(parser.feed(frame))
        assert len(events) == 1
        assert isinstance(events[0], TokenEvent)
        assert events[0].text == "Hello"

    def test_done_event(self) -> None:
        parser = SSEParser()
        data = json.dumps({
            "content": "Full reply here",
            "correlation_id": "corr-1",
            "session_id": "sess-1",
        })
        frame = f"event: done\ndata: {data}\n\n".encode()
        events = list(parser.feed(frame))
        assert len(events) == 1
        assert isinstance(events[0], DoneEvent)
        assert events[0].content == "Full reply here"
        assert events[0].correlation_id == "corr-1"
        assert events[0].session_id == "sess-1"

    def test_progress_event(self) -> None:
        parser = SSEParser()
        data = json.dumps({"event": "tool_call", "detail": "web_search"})
        frame = f"event: progress\ndata: {data}\n\n".encode()
        events = list(parser.feed(frame))
        assert len(events) == 1
        assert isinstance(events[0], ProgressEvent)
        assert events[0].event == "tool_call"
        assert events[0].detail == "web_search"

    def test_error_event(self) -> None:
        parser = SSEParser()
        data = json.dumps({"error": "Request timed out", "correlation_id": "c-2"})
        frame = f"event: error\ndata: {data}\n\n".encode()
        events = list(parser.feed(frame))
        assert len(events) == 1
        assert isinstance(events[0], ErrorEvent)
        assert events[0].error == "Request timed out"
        assert events[0].correlation_id == "c-2"

    def test_keepalive_comment(self) -> None:
        parser = SSEParser()
        frame = b": keepalive\n\n"
        events = list(parser.feed(frame))
        assert len(events) == 1
        assert isinstance(events[0], Keepalive)

    def test_multiple_events_in_one_frame(self) -> None:
        parser = SSEParser()
        frame = (
            b'event: token\ndata: {"t": "A"}\n\n'
            b'event: token\ndata: {"t": "B"}\n\n'
        )
        events = list(parser.feed(frame))
        assert len(events) == 2
        assert events[0].text == "A"
        assert events[1].text == "B"


# ---------------------------------------------------------------------------
# SSEParser — partial chunks (network fragmentation)
# ---------------------------------------------------------------------------


class TestSSEParserPartialChunks:
    """Feed fragmented bytes and verify correct reassembly."""

    def test_split_in_middle_of_line(self) -> None:
        parser = SSEParser()
        # Split "event: token\n" across two chunks
        events1 = list(parser.feed(b"event: to"))
        assert events1 == []

        events2 = list(parser.feed(b'ken\ndata: {"t": "X"}\n\n'))
        assert len(events2) == 1
        assert isinstance(events2[0], TokenEvent)
        assert events2[0].text == "X"

    def test_split_at_newline_boundary(self) -> None:
        parser = SSEParser()
        events1 = list(parser.feed(b"event: token\n"))
        assert events1 == []

        events2 = list(parser.feed(b'data: {"t": "Y"}\n'))
        assert events2 == []

        events3 = list(parser.feed(b"\n"))
        assert len(events3) == 1
        assert events3[0].text == "Y"

    def test_byte_by_byte(self) -> None:
        parser = SSEParser()
        frame = b'event: token\ndata: {"t": "Z"}\n\n'
        events: list = []
        for byte in frame:
            events.extend(parser.feed(bytes([byte])))
        assert len(events) == 1
        assert events[0].text == "Z"

    def test_split_keepalive(self) -> None:
        parser = SSEParser()
        events1 = list(parser.feed(b": keep"))
        assert events1 == []
        events2 = list(parser.feed(b"alive\n\n"))
        assert len(events2) == 1
        assert isinstance(events2[0], Keepalive)


# ---------------------------------------------------------------------------
# SSEParser — multi-byte UTF-8
# ---------------------------------------------------------------------------


class TestSSEParserUTF8:
    """Test multi-byte character handling across chunk boundaries."""

    def test_emoji_in_token(self) -> None:
        parser = SSEParser()
        data = json.dumps({"t": "Hello \U0001f600"})
        frame = f"event: token\ndata: {data}\n\n".encode("utf-8")
        events = list(parser.feed(frame))
        assert len(events) == 1
        assert events[0].text == "Hello \U0001f600"

    def test_multibyte_split_across_chunks(self) -> None:
        parser = SSEParser()
        data = json.dumps({"t": "\u00e9"})  # e-acute = 2 bytes in UTF-8
        frame = f"event: token\ndata: {data}\n\n".encode("utf-8")
        # Split in the middle of the UTF-8 sequence of the JSON string
        mid = len(frame) // 2
        events1 = list(parser.feed(frame[:mid]))
        events2 = list(parser.feed(frame[mid:]))
        all_events = events1 + events2
        assert len(all_events) == 1
        assert all_events[0].text == "\u00e9"

    def test_cjk_characters(self) -> None:
        parser = SSEParser()
        data = json.dumps({"t": "\u4f60\u597d"})  # nihao
        frame = f"event: token\ndata: {data}\n\n".encode("utf-8")
        events = list(parser.feed(frame))
        assert events[0].text == "\u4f60\u597d"


# ---------------------------------------------------------------------------
# SSEParser — edge cases
# ---------------------------------------------------------------------------


class TestSSEParserEdgeCases:
    """Edge case handling."""

    def test_unknown_event_type_ignored(self) -> None:
        parser = SSEParser()
        frame = b'event: unknown_type\ndata: {"x": 1}\n\n'
        events = list(parser.feed(frame))
        assert events == []

    def test_data_without_event_ignored(self) -> None:
        parser = SSEParser()
        frame = b'data: {"t": "orphan"}\n\n'
        events = list(parser.feed(frame))
        assert events == []

    def test_event_without_data_ignored(self) -> None:
        parser = SSEParser()
        frame = b"event: token\n\n"
        events = list(parser.feed(frame))
        assert events == []

    def test_malformed_json_ignored(self) -> None:
        parser = SSEParser()
        frame = b"event: token\ndata: {not json}\n\n"
        events = list(parser.feed(frame))
        assert events == []

    def test_empty_feed(self) -> None:
        parser = SSEParser()
        events = list(parser.feed(b""))
        assert events == []

    def test_only_newlines(self) -> None:
        parser = SSEParser()
        events = list(parser.feed(b"\n\n\n\n"))
        assert events == []

    def test_comment_without_space(self) -> None:
        parser = SSEParser()
        frame = b":keepalive\n\n"
        events = list(parser.feed(frame))
        assert len(events) == 1
        assert isinstance(events[0], Keepalive)

    def test_reset_clears_state(self) -> None:
        parser = SSEParser()
        # Feed partial data
        list(parser.feed(b"event: token\n"))
        parser.reset()
        # Now feed a complete different event
        events = list(parser.feed(b'event: token\ndata: {"t": "after"}\n\n'))
        assert len(events) == 1
        assert events[0].text == "after"

    def test_progress_missing_detail_defaults_empty(self) -> None:
        parser = SSEParser()
        data = json.dumps({"event": "thinking"})
        frame = f"event: progress\ndata: {data}\n\n".encode()
        events = list(parser.feed(frame))
        assert len(events) == 1
        assert isinstance(events[0], ProgressEvent)
        assert events[0].event == "thinking"
        assert events[0].detail == ""

    def test_cr_lf_line_endings(self) -> None:
        parser = SSEParser()
        frame = b'event: token\r\ndata: {"t": "crlf"}\r\n\r\n'
        events = list(parser.feed(frame))
        assert len(events) == 1
        assert events[0].text == "crlf"
