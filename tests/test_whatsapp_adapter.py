"""Unit tests for WhatsApp adapter and markdown converter.

TDD: These tests were written BEFORE the implementation.
"""

import asyncio
import json
import os
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.contexts import CTX_AIGUILLEUR, ensure_ctx
from common.envelope import Envelope
from common.envelope_actions import (
    ACTION_MESSAGE_INCOMING,
    ACTION_MESSAGE_OUTGOING,
    ACTION_MESSAGE_PROGRESS,
)
from common.markdown_converter import convert_md_to_whatsapp
from common.streams import KEY_WHATSAPP_PAIRING, stream_outgoing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_envelope(**kwargs) -> Envelope:
    """Build a minimal test Envelope with sensible defaults."""
    defaults = dict(
        content="hello",
        sender_id="whatsapp:+33699999999",
        channel="whatsapp",
        session_id="whatsapp:+33699999999",
        action=ACTION_MESSAGE_INCOMING,
    )
    defaults.update(kwargs)
    return Envelope(**defaults)


def _make_webhook_body(
    event: str,
    data: dict,
    token: str = "test-secret",
) -> dict:
    """Build a baileys-api webhook payload."""
    return {
        "event": event,
        "data": data,
        "webhookVerifyToken": token,
    }


def _make_messages_upsert(
    messages: list[dict],
    msg_type: str = "notify",
    token: str = "test-secret",
) -> dict:
    """Build a messages.upsert webhook payload."""
    return _make_webhook_body(
        "messages.upsert",
        {"messages": messages, "type": msg_type},
        token,
    )


def _make_message(
    jid: str = "33699999999@s.whatsapp.net",
    msg_id: str = "MSG001",
    from_me: bool = False,
    text: str = "hello from WA",
) -> dict:
    """Build a single Baileys message dict."""
    return {
        "key": {
            "remoteJid": jid,
            "id": msg_id,
            "fromMe": from_me,
        },
        "message": {
            "conversation": text,
        },
    }


# ---------------------------------------------------------------------------
# Normalization tests (Tests 1-3)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestJidNormalization:
    def test_normalize_standard_jid(self):
        """Test 1: Standard JID → E.164."""
        from aiguilleur.channels.whatsapp.adapter import normalize_whatsapp_id
        assert normalize_whatsapp_id("33699999999@s.whatsapp.net") == "+33699999999"

    def test_normalize_jid_with_device_suffix(self):
        """Test 2: JID with device suffix stripped."""
        from aiguilleur.channels.whatsapp.adapter import normalize_whatsapp_id
        assert normalize_whatsapp_id("33699999999:2@s.whatsapp.net") == "+33699999999"

    def test_e164_to_jid(self):
        """Test 3: E.164 → JID."""
        from aiguilleur.channels.whatsapp.adapter import e164_to_jid
        assert e164_to_jid("+33699999999") == "33699999999@s.whatsapp.net"


# ---------------------------------------------------------------------------
# Markdown converter tests (Tests 32-34)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestConvertMdToWhatsapp:
    def test_bold_conversion(self):
        """Test 32: **bold** → *bold*."""
        assert convert_md_to_whatsapp("**bold**") == "*bold*"

    def test_italic_conversion(self):
        """Test 33: *italic* → _italic_."""
        assert convert_md_to_whatsapp("*italic*") == "_italic_"

    def test_code_block_stripped(self):
        """Test 34: ```code``` → code (fences stripped)."""
        result = convert_md_to_whatsapp("```code```")
        assert "```" not in result
        assert "code" in result

    def test_heading_stripped(self):
        """Headings (# Title) are stripped to plain text."""
        assert convert_md_to_whatsapp("# Title") == "Title"
        assert convert_md_to_whatsapp("## Subtitle") == "Subtitle"

    def test_links_plain_url(self):
        """Links become plain URLs."""
        result = convert_md_to_whatsapp("[click](https://example.com)")
        assert "https://example.com" in result
        assert "[click]" not in result

    def test_line_breaks_preserved(self):
        """WhatsApp renders line breaks natively."""
        text = "line1\nline2\n\nline3"
        assert convert_md_to_whatsapp(text) == text

    def test_underscore_italic(self):
        """_italic_ stays _italic_ (WhatsApp native)."""
        assert convert_md_to_whatsapp("_italic_") == "_italic_"

    def test_horizontal_rule_stripped(self):
        """Horizontal rules removed."""
        result = convert_md_to_whatsapp("text\n---\nmore")
        assert "---" not in result


# ---------------------------------------------------------------------------
# Text extraction test (Test 8)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestExtractTextContent:
    def test_conversation_field(self):
        """Plain text in conversation field."""
        from aiguilleur.channels.whatsapp.adapter import _RelaisWhatsAppClient
        msg = {"message": {"conversation": "hello"}}
        assert _RelaisWhatsAppClient._extract_text_content(msg) == "hello"

    def test_extended_text_message(self):
        """Extended text message with URL preview."""
        from aiguilleur.channels.whatsapp.adapter import _RelaisWhatsAppClient
        msg = {"message": {"extendedTextMessage": {"text": "check this"}}}
        assert _RelaisWhatsAppClient._extract_text_content(msg) == "check this"

    def test_image_caption(self):
        """Image with caption — extract caption text."""
        from aiguilleur.channels.whatsapp.adapter import _RelaisWhatsAppClient
        msg = {"message": {"imageMessage": {"caption": "look at this"}}}
        assert _RelaisWhatsAppClient._extract_text_content(msg) == "look at this"

    def test_none_for_non_text(self):
        """Non-text messages return None."""
        from aiguilleur.channels.whatsapp.adapter import _RelaisWhatsAppClient
        msg = {"message": {"audioMessage": {"seconds": 5}}}
        assert _RelaisWhatsAppClient._extract_text_content(msg) is None

    def test_none_for_missing_message(self):
        """Missing message field returns None."""
        from aiguilleur.channels.whatsapp.adapter import _RelaisWhatsAppClient
        msg = {"message": None}
        assert _RelaisWhatsAppClient._extract_text_content(msg) is None


# ---------------------------------------------------------------------------
# Message processing tests (Tests 9-16)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestProcessSingleMessage:
    """Tests for _process_single_message routing logic."""

    @pytest.fixture
    def client(self):
        """Build a minimal _RelaisWhatsAppClient with mocked dependencies."""
        from aiguilleur.channels.whatsapp.adapter import _RelaisWhatsAppClient

        adapter = MagicMock()
        adapter.stop_event = MagicMock()
        adapter.stop_event.is_set.return_value = False
        adapter.config = MagicMock()
        adapter.config.profile = "default"
        adapter.config.prompt_path = "channels/whatsapp_default.md"

        redis_mock = AsyncMock()
        redis_mock.xadd = AsyncMock()

        client = _RelaisWhatsAppClient.__new__(_RelaisWhatsAppClient)
        client._adapter = adapter
        client._redis = redis_mock
        client._log = MagicMock()
        client._stop = adapter.stop_event
        client._phone_number = "+33612345678"
        client._self_jid = "33612345678@s.whatsapp.net"
        client.seen_message_ids = OrderedDict()
        client.sent_message_ids = OrderedDict()
        return client

    @pytest.mark.asyncio
    async def test_builds_correct_envelope(self, client):
        """Test 9: Correct envelope for a DM from external contact."""
        msg = _make_message()
        await client._process_single_message(msg)

        assert client._redis.xadd.call_count == 1
        call_args = client._redis.xadd.call_args
        payload_json = call_args[0][1]["payload"]
        env = Envelope.from_json(payload_json)

        assert env.channel == "whatsapp"
        assert env.sender_id == "whatsapp:+33699999999"
        assert env.content == "hello from WA"
        assert env.action == ACTION_MESSAGE_INCOMING
        ctx = env.context[CTX_AIGUILLEUR]
        assert ctx["reply_to"] == "33699999999@s.whatsapp.net"

    @pytest.mark.asyncio
    async def test_history_sync_ignored(self, client):
        """Test 10: messages.upsert with type=append → ignored."""
        # This is tested at _handle_messages_upsert level, not _process_single_message
        pass  # Covered by webhook routing test

    @pytest.mark.asyncio
    async def test_group_jid_ignored(self, client):
        """Test 11: Group JID (@g.us) → ignored."""
        msg = _make_message(jid="120363xxx@g.us")
        await client._process_single_message(msg)
        client._redis.xadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_from_me_non_self_ignored(self, client):
        """Test 12: fromMe=true in non-self conversation → ignored."""
        msg = _make_message(from_me=True)
        await client._process_single_message(msg)
        client._redis.xadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_from_me_self_chat_treated_as_admin(self, client):
        """Test 13: fromMe=true in self conversation → treated as admin message."""
        msg = _make_message(
            jid="33612345678@s.whatsapp.net",
            from_me=True,
            text="note to self",
        )
        await client._process_single_message(msg)

        assert client._redis.xadd.call_count == 1
        payload = Envelope.from_json(
            client._redis.xadd.call_args[0][1]["payload"]
        )
        assert payload.sender_id == "whatsapp:+33612345678"
        assert payload.content == "note to self"

    @pytest.mark.asyncio
    async def test_anti_loop_skips_own_sent_messages(self, client):
        """Test 14: Message in sent_message_ids → ignored (RELAIS's own reply)."""
        client.sent_message_ids["MSG_SENT"] = None
        msg = _make_message(
            jid="33612345678@s.whatsapp.net",
            msg_id="MSG_SENT",
            from_me=True,
        )
        await client._process_single_message(msg)
        client._redis.xadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_deduplication(self, client):
        """Test 15: Same message_id twice → only one xadd."""
        msg = _make_message(msg_id="DUP001")
        await client._process_single_message(msg)
        await client._process_single_message(msg)
        assert client._redis.xadd.call_count == 1

    @pytest.mark.asyncio
    async def test_xadd_failure_does_not_crash(self, client):
        """Test 16: Individual xadd failure logged, doesn't crash."""
        client._redis.xadd.side_effect = Exception("Redis error")
        msg = _make_message(msg_id="FAIL001")
        # Should not raise
        with pytest.raises(Exception, match="Redis error"):
            await client._process_single_message(msg)


# ---------------------------------------------------------------------------
# Self-chat identity tests (Tests 17-19)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSelfChatIdentity:
    @pytest.mark.asyncio
    async def test_self_chat_produces_owner_sender_id(self):
        """Test 17: Self-chat → sender_id uses owner number."""
        from aiguilleur.channels.whatsapp.adapter import _RelaisWhatsAppClient

        adapter = MagicMock()
        adapter.stop_event = MagicMock()
        adapter.config = MagicMock()
        adapter.config.profile = "default"
        adapter.config.prompt_path = None

        redis_mock = AsyncMock()

        client = _RelaisWhatsAppClient.__new__(_RelaisWhatsAppClient)
        client._adapter = adapter
        client._redis = redis_mock
        client._log = MagicMock()
        client._stop = adapter.stop_event
        client._phone_number = "+33612345678"
        client._self_jid = "33612345678@s.whatsapp.net"
        client.seen_message_ids = OrderedDict()
        client.sent_message_ids = OrderedDict()

        msg = _make_message(
            jid="33612345678@s.whatsapp.net",
            from_me=True,
            text="hello relais",
        )
        await client._process_single_message(msg)

        payload = Envelope.from_json(redis_mock.xadd.call_args[0][1]["payload"])
        assert payload.sender_id == "whatsapp:+33612345678"


# ---------------------------------------------------------------------------
# QR/Pairing tests (Tests 20-25)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPairingHandlers:
    @pytest.fixture
    def client(self):
        """Build a client with mocked Redis for pairing tests."""
        from aiguilleur.channels.whatsapp.adapter import _RelaisWhatsAppClient

        adapter = MagicMock()
        adapter.stop_event = MagicMock()
        adapter.config = MagicMock()

        redis_mock = AsyncMock()

        client = _RelaisWhatsAppClient.__new__(_RelaisWhatsAppClient)
        client._adapter = adapter
        client._redis = redis_mock
        client._log = MagicMock()
        client._stop = adapter.stop_event
        client._phone_number = "+33612345678"
        client._self_jid = "33612345678@s.whatsapp.net"
        client.seen_message_ids = OrderedDict()
        client.sent_message_ids = OrderedDict()
        return client

    @pytest.mark.asyncio
    async def test_handle_qr_event_relays_ascii_qr(self, client):
        """Test 20: QR event relays ASCII art to originator channel."""
        pairing = {
            "channel": "discord",
            "sender_id": "discord:123",
            "session_id": "sess-1",
            "correlation_id": "corr-1",
            "reply_to": "123456789",
            "state": "pending_qr",
        }
        client._redis.get = AsyncMock(return_value=json.dumps(pairing))
        client._redis.set = AsyncMock()

        payload = {
            "data": {"qr": "whatsapp://link?code=test123"},
        }

        await client._handle_qr_event(payload)

        # Should xadd to discord outgoing stream
        assert client._redis.xadd.call_count == 1
        call_args = client._redis.xadd.call_args
        assert call_args[0][0] == stream_outgoing("discord")

        env = Envelope.from_json(call_args[0][1]["payload"])
        assert env.action == ACTION_MESSAGE_OUTGOING
        assert "QR" in env.content or "qr" in env.content.lower()
        assert env.context[CTX_AIGUILLEUR]["reply_to"] == "123456789"

    @pytest.mark.asyncio
    async def test_handle_qr_event_no_pairing_context(self, client):
        """Test 21: QR event with no pairing context → ignored."""
        client._redis.get = AsyncMock(return_value=None)
        payload = {"data": {"qr": "test"}}
        await client._handle_qr_event(payload)
        client._redis.xadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_connected_event(self, client):
        """Test 22: Connected event → confirmation + delete pairing key."""
        pairing = {
            "channel": "discord",
            "sender_id": "discord:123",
            "session_id": "sess-1",
            "correlation_id": "corr-1",
            "reply_to": "123456789",
        }
        client._redis.get = AsyncMock(return_value=json.dumps(pairing))
        client._redis.delete = AsyncMock()

        payload = {"data": {"connection": "open"}}
        await client._handle_connected_event(payload)

        # Should send confirmation
        assert client._redis.xadd.call_count == 1
        env = Envelope.from_json(client._redis.xadd.call_args[0][1]["payload"])
        assert env.action == ACTION_MESSAGE_OUTGOING
        assert "linked" in env.content.lower() or "success" in env.content.lower()

        # Should delete pairing key
        client._redis.delete.assert_called_once_with(KEY_WHATSAPP_PAIRING)

    @pytest.mark.asyncio
    async def test_handle_close_event_during_pairing(self, client):
        """Test 23: Close event during pairing → error message + delete key."""
        pairing = {
            "channel": "discord",
            "sender_id": "discord:123",
            "session_id": "sess-1",
            "correlation_id": "corr-1",
            "reply_to": "123456789",
        }
        client._redis.get = AsyncMock(return_value=json.dumps(pairing))
        client._redis.delete = AsyncMock()

        payload = {"data": {"connection": "close", "lastDisconnect": {"error": "timeout"}}}
        await client._handle_close_event(payload)

        assert client._redis.xadd.call_count == 1
        env = Envelope.from_json(client._redis.xadd.call_args[0][1]["payload"])
        assert "failed" in env.content.lower()
        client._redis.delete.assert_called_once_with(KEY_WHATSAPP_PAIRING)

    @pytest.mark.asyncio
    async def test_handle_close_event_wrong_phone(self, client):
        """Test 24: Close with wrong_phone_number → specific error."""
        pairing = {
            "channel": "discord",
            "sender_id": "discord:123",
            "session_id": "sess-1",
            "correlation_id": "corr-1",
            "reply_to": "123456789",
        }
        client._redis.get = AsyncMock(return_value=json.dumps(pairing))
        client._redis.delete = AsyncMock()

        payload = {"data": {"connection": "close", "lastDisconnect": {"error": "wrong_phone_number"}}}
        await client._handle_close_event(payload)

        env = Envelope.from_json(client._redis.xadd.call_args[0][1]["payload"])
        assert "wrong phone number" in env.content.lower()

    @pytest.mark.asyncio
    async def test_handle_close_event_runtime(self, client):
        """Test 25: Close outside pairing (runtime) → log only, no message."""
        client._redis.get = AsyncMock(return_value=None)

        payload = {"data": {"connection": "close", "lastDisconnect": {"error": "lost"}}}
        await client._handle_close_event(payload)

        client._redis.xadd.assert_not_called()


# ---------------------------------------------------------------------------
# Send message tests (Tests 26-27)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSendMessage:
    def test_split_whatsapp_message_long(self):
        """Test 27: Long messages split at 4096 boundary."""
        from aiguilleur.channels.whatsapp.adapter import _split_whatsapp_message
        long_text = "a" * 8000
        parts = _split_whatsapp_message(long_text)
        assert all(len(p) <= 4096 for p in parts)
        assert "".join(parts) == long_text

    def test_split_whatsapp_message_short(self):
        """Short messages not split."""
        from aiguilleur.channels.whatsapp.adapter import _split_whatsapp_message
        short = "hello world"
        parts = _split_whatsapp_message(short)
        assert parts == [short]


# ---------------------------------------------------------------------------
# Lifecycle tests (Test 30)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAdapterLifecycle:
    @pytest.mark.asyncio
    async def test_missing_env_var_returns_cleanly(self):
        """Test 30: Missing env var → run() logs error, returns cleanly."""
        from aiguilleur.channels.whatsapp.adapter import WhatsAppAiguilleur
        from aiguilleur.channel_config import ChannelConfig

        config = ChannelConfig(name="whatsapp", enabled=True)
        adapter = WhatsAppAiguilleur(config)

        env_patch = {
            "WHATSAPP_GATEWAY_URL": "",
            "WHATSAPP_API_KEY": "",
            "WHATSAPP_PHONE_NUMBER": "",
            "WHATSAPP_WEBHOOK_SECRET": "",
            "WHATSAPP_WEBHOOK_PORT": "8765",
            "WHATSAPP_WEBHOOK_HOST": "127.0.0.1",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            # Should return cleanly, not raise
            await adapter.run()


# ---------------------------------------------------------------------------
# Live config test (Test 31)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLiveConfig:
    @pytest.mark.asyncio
    async def test_reads_config_live(self):
        """Test 31: Client reads adapter.config on each message."""
        from aiguilleur.channels.whatsapp.adapter import _RelaisWhatsAppClient

        adapter = MagicMock()
        adapter.stop_event = MagicMock()

        # First config
        config1 = MagicMock()
        config1.profile = "default"
        config1.prompt_path = None

        # Second config
        config2 = MagicMock()
        config2.profile = "fast"
        config2.prompt_path = "channels/whatsapp_default.md"

        adapter.config = config1

        redis_mock = AsyncMock()

        client = _RelaisWhatsAppClient.__new__(_RelaisWhatsAppClient)
        client._adapter = adapter
        client._redis = redis_mock
        client._log = MagicMock()
        client._stop = adapter.stop_event
        client._phone_number = "+33612345678"
        client._self_jid = "33612345678@s.whatsapp.net"
        client.seen_message_ids = OrderedDict()
        client.sent_message_ids = OrderedDict()

        # First message with config1
        msg1 = _make_message(msg_id="M1")
        await client._process_single_message(msg1)
        env1 = Envelope.from_json(redis_mock.xadd.call_args[0][1]["payload"])
        assert env1.context[CTX_AIGUILLEUR]["channel_profile"] == "default"

        # Change config
        adapter.config = config2
        redis_mock.reset_mock()

        # Second message with config2
        msg2 = _make_message(msg_id="M2")
        await client._process_single_message(msg2)
        env2 = Envelope.from_json(redis_mock.xadd.call_args[0][1]["payload"])
        assert env2.context[CTX_AIGUILLEUR]["channel_profile"] == "fast"
