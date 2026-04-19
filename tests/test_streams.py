"""Unit tests for common/streams.py helpers.

TDD: Tests written BEFORE the implementation (RED phase).
"""

from __future__ import annotations

import pytest


class TestStreamOutgoing:
    def test_returns_channel_stream_name(self):
        """stream_outgoing returns relais:messages:outgoing:{channel}."""
        from common.streams import stream_outgoing

        assert stream_outgoing("discord") == "relais:messages:outgoing:discord"

    def test_returns_rest_channel(self):
        """stream_outgoing works for rest channel."""
        from common.streams import stream_outgoing

        assert stream_outgoing("rest") == "relais:messages:outgoing:rest"


class TestStreamOutgoingUser:
    def test_returns_user_scoped_stream_name(self):
        """stream_outgoing_user returns relais:messages:outgoing:{channel}:{user_id}."""
        from common.streams import stream_outgoing_user

        result = stream_outgoing_user("rest", "usr_admin")
        assert result == "relais:messages:outgoing:rest:usr_admin"

    def test_different_channels(self):
        """stream_outgoing_user handles arbitrary channel names."""
        from common.streams import stream_outgoing_user

        assert stream_outgoing_user("telegram", "usr_abc") == "relais:messages:outgoing:telegram:usr_abc"

    def test_user_id_with_special_chars(self):
        """stream_outgoing_user preserves user_id with underscores and hyphens."""
        from common.streams import stream_outgoing_user

        assert stream_outgoing_user("rest", "usr_test-42") == "relais:messages:outgoing:rest:usr_test-42"

    def test_empty_user_id_still_works(self):
        """stream_outgoing_user with empty user_id still produces a valid string."""
        from common.streams import stream_outgoing_user

        result = stream_outgoing_user("rest", "")
        assert result == "relais:messages:outgoing:rest:"

    def test_return_type_is_str(self):
        """stream_outgoing_user always returns a str."""
        from common.streams import stream_outgoing_user

        result = stream_outgoing_user("rest", "usr_x")
        assert isinstance(result, str)
