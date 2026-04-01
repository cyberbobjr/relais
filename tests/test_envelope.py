"""Unit tests for common/envelope.py — Envelope and MediaRef dataclasses."""
import json
import pytest

from common.envelope import Envelope, MediaRef


@pytest.mark.unit
class TestEnvelopeFromParent:
    def _make_parent(self) -> Envelope:
        return Envelope(
            content="hello",
            sender_id="discord:123",
            channel="discord",
            session_id="sess-1",
            metadata={"key": "value", "traces": [{"brick": "portail", "action": "enrich"}]},
        )

    def test_inherits_tracking_fields(self):
        parent = self._make_parent()
        child = Envelope.from_parent(parent, "reply")
        assert child.sender_id == parent.sender_id
        assert child.channel == parent.channel
        assert child.session_id == parent.session_id
        assert child.correlation_id == parent.correlation_id

    def test_metadata_is_deep_copied(self):
        """Mutating child metadata must not affect parent — traces list included."""
        parent = self._make_parent()
        child = Envelope.from_parent(parent, "reply")

        # Mutate child's nested list
        child.metadata["traces"].append({"brick": "atelier", "action": "generate"})

        assert len(parent.metadata["traces"]) == 1, (
            "parent traces list was mutated — from_parent used shallow copy"
        )

    def test_metadata_top_level_isolation(self):
        parent = self._make_parent()
        child = Envelope.from_parent(parent, "reply")
        child.metadata["new_key"] = "new_value"
        assert "new_key" not in parent.metadata

    def test_new_content(self):
        parent = self._make_parent()
        child = Envelope.from_parent(parent, "world")
        assert child.content == "world"
        assert parent.content == "hello"


@pytest.mark.unit
class TestEnvelopeFromJson:
    def _base_dict(self) -> dict:
        return {
            "content": "test",
            "sender_id": "telegram:42",
            "channel": "telegram",
            "session_id": "sess-2",
            "correlation_id": "corr-abc",
            "timestamp": 1700000000.0,
            "metadata": {"foo": "bar"},
            "media_refs": [],
        }

    def test_roundtrip(self):
        env = Envelope(
            content="ping",
            sender_id="discord:1",
            channel="discord",
            session_id="s1",
        )
        restored = Envelope.from_json(env.to_json())
        assert restored.content == env.content
        assert restored.sender_id == env.sender_id
        assert restored.correlation_id == env.correlation_id

    def test_unknown_fields_ignored(self):
        data = self._base_dict()
        data["unknown_future_field"] = "should be ignored"
        restored = Envelope.from_json(json.dumps(data))
        assert restored.content == "test"
        assert not hasattr(restored, "unknown_future_field")

    def test_media_refs_deserialized(self):
        data = self._base_dict()
        data["media_refs"] = [
            {
                "media_id": "m1",
                "path": "/tmp/img.jpg",
                "mime_type": "image/jpeg",
                "size_bytes": 1024,
                "expires_in_hours": 24,
            }
        ]
        restored = Envelope.from_json(json.dumps(data))
        assert len(restored.media_refs) == 1
        assert restored.media_refs[0].media_id == "m1"

    def test_missing_media_refs_defaults_to_empty(self):
        data = self._base_dict()
        del data["media_refs"]
        restored = Envelope.from_json(json.dumps(data))
        assert restored.media_refs == []


@pytest.mark.unit
class TestEnvelopeAddTrace:
    def test_trace_appended(self):
        env = Envelope(
            content="msg",
            sender_id="discord:1",
            channel="discord",
            session_id="s1",
        )
        env.add_trace("portail", "enrich")
        env.add_trace("atelier", "generate")
        assert len(env.metadata["traces"]) == 2
        assert env.metadata["traces"][0]["brick"] == "portail"
        assert env.metadata["traces"][1]["brick"] == "atelier"

    def test_trace_creates_key_if_absent(self):
        env = Envelope(
            content="msg",
            sender_id="discord:1",
            channel="discord",
            session_id="s1",
        )
        assert "traces" not in env.metadata
        env.add_trace("sentinelle", "check")
        assert "traces" in env.metadata
