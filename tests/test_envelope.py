"""Unit tests for common/envelope.py — Envelope and MediaRef dataclasses."""
import json
import pytest

from common.envelope import Envelope, MediaRef
from common.envelope_actions import ACTION_MESSAGE_INCOMING, ACTION_MESSAGE_VALIDATED
from common.contexts import CTX_AIGUILLEUR, CTX_PORTAIL, ensure_ctx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_envelope(**kwargs) -> Envelope:
    defaults = dict(
        content="hello",
        sender_id="discord:123",
        channel="discord",
        session_id="sess-1",
    )
    defaults.update(kwargs)
    return Envelope(**defaults)


# ---------------------------------------------------------------------------
# from_parent
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestEnvelopeFromParent:
    def test_inherits_tracking_fields(self):
        parent = _make_envelope(action=ACTION_MESSAGE_INCOMING)
        child = Envelope.from_parent(parent, "reply")
        assert child.sender_id == parent.sender_id
        assert child.channel == parent.channel
        assert child.session_id == parent.session_id
        assert child.correlation_id == parent.correlation_id

    def test_does_not_inherit_action(self):
        """Each producing brick sets its own action — child starts blank."""
        parent = _make_envelope(action=ACTION_MESSAGE_INCOMING)
        child = Envelope.from_parent(parent, "reply")
        assert child.action == ""

    def test_context_is_deep_copied(self):
        parent = _make_envelope()
        ensure_ctx(parent, CTX_AIGUILLEUR)["channel_profile"] = "default"
        child = Envelope.from_parent(parent, "reply")

        child.context[CTX_AIGUILLEUR]["channel_profile"] = "fast"

        assert parent.context[CTX_AIGUILLEUR]["channel_profile"] == "default", (
            "parent context was mutated — from_parent used shallow copy"
        )

    def test_context_new_key_isolation(self):
        parent = _make_envelope()
        child = Envelope.from_parent(parent, "reply")
        ensure_ctx(child, CTX_PORTAIL)["user_id"] = "usr_admin"
        assert CTX_PORTAIL not in parent.context

    def test_traces_are_deep_copied(self):
        parent = _make_envelope()
        parent.add_trace("aiguilleur", "incoming")
        child = Envelope.from_parent(parent, "reply")

        child.add_trace("portail", "enriched")

        assert len(parent.traces) == 1, (
            "parent traces list was mutated — from_parent used shallow copy"
        )

    def test_new_content(self):
        parent = _make_envelope()
        child = Envelope.from_parent(parent, "world")
        assert child.content == "world"
        assert parent.content == "hello"


# ---------------------------------------------------------------------------
# from_json / to_json round-trip
# ---------------------------------------------------------------------------

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
            "action": ACTION_MESSAGE_INCOMING,
            "traces": [],
            "context": {CTX_AIGUILLEUR: {"channel_profile": "default"}},
            "media_refs": [],
        }

    def test_roundtrip(self):
        env = _make_envelope(action=ACTION_MESSAGE_INCOMING)
        ensure_ctx(env, CTX_AIGUILLEUR)["channel_profile"] = "default"
        env.add_trace("aiguilleur", "incoming")

        restored = Envelope.from_json(env.to_json())

        assert restored.content == env.content
        assert restored.sender_id == env.sender_id
        assert restored.correlation_id == env.correlation_id
        assert restored.action == ACTION_MESSAGE_INCOMING
        assert len(restored.traces) == 1
        assert restored.traces[0]["brick"] == "aiguilleur"
        assert restored.context[CTX_AIGUILLEUR]["channel_profile"] == "default"

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

    def test_action_preserved_in_roundtrip(self):
        env = _make_envelope(action=ACTION_MESSAGE_VALIDATED)
        restored = Envelope.from_json(env.to_json())
        assert restored.action == ACTION_MESSAGE_VALIDATED

    def test_context_preserved_in_roundtrip(self):
        env = _make_envelope()
        ensure_ctx(env, CTX_PORTAIL).update({"user_id": "usr_admin", "llm_profile": "precise"})
        restored = Envelope.from_json(env.to_json())
        assert restored.context[CTX_PORTAIL]["user_id"] == "usr_admin"
        assert restored.context[CTX_PORTAIL]["llm_profile"] == "precise"


# ---------------------------------------------------------------------------
# add_trace
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestEnvelopeAddTrace:
    def test_trace_appended(self):
        env = _make_envelope()
        env.add_trace("portail", "enriched")
        env.add_trace("atelier", "generated")
        assert len(env.traces) == 2
        assert env.traces[0]["brick"] == "portail"
        assert env.traces[0]["step"] == "enriched"
        assert env.traces[1]["brick"] == "atelier"

    def test_trace_starts_empty(self):
        env = _make_envelope()
        assert env.traces == []
        env.add_trace("sentinelle", "acl_check")
        assert len(env.traces) == 1

    def test_trace_has_timestamp(self):
        env = _make_envelope()
        env.add_trace("portail", "enriched")
        assert "timestamp" in env.traces[0]
        assert isinstance(env.traces[0]["timestamp"], float)


# ---------------------------------------------------------------------------
# ensure_ctx helper
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestEnsureCtx:
    def test_creates_namespace_if_absent(self):
        env = _make_envelope()
        ctx = ensure_ctx(env, CTX_PORTAIL)
        assert CTX_PORTAIL in env.context
        assert isinstance(ctx, dict)

    def test_idempotent(self):
        env = _make_envelope()
        ctx1 = ensure_ctx(env, CTX_PORTAIL)
        ctx1["user_id"] = "usr_admin"
        ctx2 = ensure_ctx(env, CTX_PORTAIL)
        assert ctx2["user_id"] == "usr_admin"
        assert ctx1 is ctx2

    def test_returns_existing_dict(self):
        env = _make_envelope()
        env.context[CTX_AIGUILLEUR] = {"channel_profile": "fast"}
        ctx = ensure_ctx(env, CTX_AIGUILLEUR)
        assert ctx["channel_profile"] == "fast"


# ---------------------------------------------------------------------------
# action field
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestEnvelopeAction:
    def test_default_is_empty_string(self):
        env = _make_envelope()
        assert env.action == ""

    def test_action_set_on_construction(self):
        env = _make_envelope(action=ACTION_MESSAGE_INCOMING)
        assert env.action == ACTION_MESSAGE_INCOMING

    def test_action_mutable(self):
        env = _make_envelope()
        env.action = ACTION_MESSAGE_VALIDATED
        assert env.action == ACTION_MESSAGE_VALIDATED
