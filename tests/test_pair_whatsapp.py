"""Unit tests for scripts/pair_whatsapp.py.

The script replaces the former /settings whatsapp handler in Commandant.
It is invoked by the relais-config subagent via its execute shell tool
as part of the channel-setup skill.
"""

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — load the script as a module (it has a hyphen-free but
# non-package filename and lives in scripts/)
# ---------------------------------------------------------------------------

def _load_pair_script():
    """Import scripts/pair_whatsapp.py as a module.

    The script is executable and lives outside the Python package tree,
    so we load it via importlib.
    """
    repo_root = Path(__file__).resolve().parent.parent
    script_path = repo_root / "scripts" / "pair_whatsapp.py"
    spec = importlib.util.spec_from_file_location("pair_whatsapp", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["pair_whatsapp"] = module
    spec.loader.exec_module(module)
    return module


pair_whatsapp = _load_pair_script()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def valid_env():
    """Complete set of environment variables."""
    return {
        "WHATSAPP_GATEWAY_URL": "http://localhost:3025",
        "WHATSAPP_API_KEY": "test-key",
        "WHATSAPP_PHONE_NUMBER": "+33612345678",
        "WHATSAPP_WEBHOOK_SECRET": "test-secret",
        "WHATSAPP_WEBHOOK_PORT": "8765",
        "WHATSAPP_WEBHOOK_HOST": "127.0.0.1",
    }


@pytest.fixture
def cli_args():
    """Complete set of CLI arguments."""
    return MagicMock(
        sender_id="discord:12345",
        channel="discord",
        session_id="sess-abc",
        correlation_id="corr-xyz",
        reply_to="12345",
    )


def _async_cm(return_value):
    """Build a mock that behaves as ``async with expr as val:``."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_aiohttp(health_status=200, gw_status=200, health_exc=None, gw_exc=None):
    """Build aiohttp mock for the health + POST calls."""
    health_resp = MagicMock()
    health_resp.status = health_status

    gw_resp = MagicMock()
    gw_resp.status = gw_status
    gw_resp.text = AsyncMock(return_value="body")

    session_mock = MagicMock()
    if health_exc:
        session_mock.get.side_effect = health_exc
    else:
        session_mock.get.return_value = _async_cm(health_resp)
    if gw_exc:
        session_mock.post.side_effect = gw_exc
    else:
        session_mock.post.return_value = _async_cm(gw_resp)

    mock_mod = MagicMock()
    mock_mod.ClientSession.return_value = _async_cm(session_mock)
    mock_mod.ClientTimeout = MagicMock()
    return mock_mod, session_mock


# ---------------------------------------------------------------------------
# PairingRoute
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPairingRoute:
    def test_to_redis_json_contains_all_fields(self):
        """JSON payload contains every required field for the adapter."""
        route = pair_whatsapp.PairingRoute(
            sender_id="discord:1",
            channel="discord",
            session_id="sess",
            correlation_id="corr",
            reply_to="1",
        )
        parsed = json.loads(route.to_redis_json())
        assert parsed["sender_id"] == "discord:1"
        assert parsed["channel"] == "discord"
        assert parsed["session_id"] == "sess"
        assert parsed["correlation_id"] == "corr"
        assert parsed["reply_to"] == "1"
        assert parsed["state"] == "pending_qr"
        assert isinstance(parsed["timestamp"], float)


# ---------------------------------------------------------------------------
# WhatsAppEnv
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestWhatsAppEnv:
    def test_from_environ_reads_all_vars(self, valid_env):
        """All env vars loaded correctly."""
        with patch.dict(os.environ, valid_env, clear=True):
            env = pair_whatsapp.WhatsAppEnv.from_environ()
        assert env.phone_number == "+33612345678"
        assert env.api_key == "test-key"
        assert env.gateway_url == "http://localhost:3025"
        assert env.webhook_host == "127.0.0.1"
        assert env.webhook_port == "8765"
        assert env.webhook_secret == "test-secret"

    def test_defaults_when_missing(self):
        """Optional env vars fall back to documented defaults."""
        with patch.dict(os.environ, {}, clear=True):
            env = pair_whatsapp.WhatsAppEnv.from_environ()
        assert env.gateway_url == "http://localhost:3025"
        assert env.webhook_host == "127.0.0.1"
        assert env.webhook_port == "8765"
        assert env.phone_number == ""
        assert env.api_key == ""

    def test_gateway_url_trailing_slash_stripped(self):
        """Trailing slash on gateway URL is stripped to avoid double slashes."""
        with patch.dict(os.environ, {"WHATSAPP_GATEWAY_URL": "http://example.com/"}, clear=True):
            env = pair_whatsapp.WhatsAppEnv.from_environ()
        assert env.gateway_url == "http://example.com"


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestParseArgs:
    def test_all_required_fields(self):
        """All CLI args parsed correctly."""
        args = pair_whatsapp.parse_args(
            [
                "--sender-id", "discord:1",
                "--channel", "discord",
                "--session-id", "sess-1",
                "--correlation-id", "corr-1",
                "--reply-to", "12345",
            ]
        )
        assert args.sender_id == "discord:1"
        assert args.channel == "discord"
        assert args.session_id == "sess-1"
        assert args.correlation_id == "corr-1"
        assert args.reply_to == "12345"

    def test_missing_required_raises(self):
        """Missing a required arg causes SystemExit."""
        with pytest.raises(SystemExit):
            pair_whatsapp.parse_args(["--channel", "discord"])


# ---------------------------------------------------------------------------
# run() — integration of the full flow
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRun:
    @pytest.mark.asyncio
    async def test_missing_phone_number_returns_bad_args(self, cli_args):
        """Empty WHATSAPP_PHONE_NUMBER → EXIT_BAD_ARGS."""
        with patch.dict(os.environ, {"WHATSAPP_API_KEY": "k", "WHATSAPP_WEBHOOK_SECRET": "s"}, clear=True):
            code = await pair_whatsapp.run(cli_args)
        assert code == pair_whatsapp.EXIT_BAD_ARGS

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_bad_args(self, cli_args):
        """Empty WHATSAPP_API_KEY → EXIT_BAD_ARGS."""
        with patch.dict(os.environ, {"WHATSAPP_PHONE_NUMBER": "+33600000000", "WHATSAPP_WEBHOOK_SECRET": "s"}, clear=True):
            code = await pair_whatsapp.run(cli_args)
        assert code == pair_whatsapp.EXIT_BAD_ARGS

    @pytest.mark.asyncio
    async def test_missing_webhook_secret_returns_bad_args(self, cli_args):
        """Empty WHATSAPP_WEBHOOK_SECRET → EXIT_BAD_ARGS."""
        with patch.dict(os.environ, {"WHATSAPP_PHONE_NUMBER": "+33600000000", "WHATSAPP_API_KEY": "k"}, clear=True):
            code = await pair_whatsapp.run(cli_args)
        assert code == pair_whatsapp.EXIT_BAD_ARGS

    @pytest.mark.asyncio
    async def test_adapter_unhealthy_returns_adapter_unreachable(self, cli_args, valid_env):
        """HTTP 503 on /health → EXIT_ADAPTER_UNREACHABLE."""
        mock_mod, _ = _mock_aiohttp(health_status=503)
        with patch.dict(os.environ, valid_env, clear=True):
            with patch.object(pair_whatsapp, "check_adapter_health", AsyncMock(return_value=(False, "HTTP 503"))):
                code = await pair_whatsapp.run(cli_args)
        assert code == pair_whatsapp.EXIT_ADAPTER_UNREACHABLE

    @pytest.mark.asyncio
    async def test_gateway_rejects_returns_gateway_failed(self, cli_args, valid_env):
        """Gateway returns HTTP 4xx → EXIT_GATEWAY_FAILED."""
        with patch.dict(os.environ, valid_env, clear=True):
            with patch.object(pair_whatsapp, "check_adapter_health", AsyncMock(return_value=(True, "ok"))):
                with patch.object(
                    pair_whatsapp,
                    "call_gateway_create_connection",
                    AsyncMock(return_value=(False, "HTTP 401: bad key")),
                ):
                    code = await pair_whatsapp.run(cli_args)
        assert code == pair_whatsapp.EXIT_GATEWAY_FAILED

    @pytest.mark.asyncio
    async def test_redis_failure_returns_redis_failed(self, cli_args, valid_env):
        """Redis write failure → EXIT_REDIS_FAILED."""
        with patch.dict(os.environ, valid_env, clear=True):
            with patch.object(pair_whatsapp, "check_adapter_health", AsyncMock(return_value=(True, "ok"))):
                with patch.object(
                    pair_whatsapp,
                    "call_gateway_create_connection",
                    AsyncMock(return_value=(True, "accepted")),
                ):
                    with patch.object(
                        pair_whatsapp,
                        "store_pairing_context",
                        AsyncMock(return_value=(False, "ConnectionError")),
                    ):
                        code = await pair_whatsapp.run(cli_args)
        assert code == pair_whatsapp.EXIT_REDIS_FAILED

    @pytest.mark.asyncio
    async def test_happy_path_returns_ok(self, cli_args, valid_env):
        """All steps succeed → EXIT_OK."""
        with patch.dict(os.environ, valid_env, clear=True):
            with patch.object(pair_whatsapp, "check_adapter_health", AsyncMock(return_value=(True, "ok"))):
                with patch.object(
                    pair_whatsapp,
                    "call_gateway_create_connection",
                    AsyncMock(return_value=(True, "accepted")),
                ):
                    with patch.object(
                        pair_whatsapp,
                        "store_pairing_context",
                        AsyncMock(return_value=(True, "stored")),
                    ):
                        code = await pair_whatsapp.run(cli_args)
        assert code == pair_whatsapp.EXIT_OK

    @pytest.mark.asyncio
    async def test_happy_path_stores_route_with_cli_values(self, cli_args, valid_env):
        """Store is called with a PairingRoute containing the CLI args."""
        captured = {}

        async def fake_store(route):
            captured["route"] = route
            return True, "stored"

        with patch.dict(os.environ, valid_env, clear=True):
            with patch.object(pair_whatsapp, "check_adapter_health", AsyncMock(return_value=(True, "ok"))):
                with patch.object(
                    pair_whatsapp,
                    "call_gateway_create_connection",
                    AsyncMock(return_value=(True, "accepted")),
                ):
                    with patch.object(pair_whatsapp, "store_pairing_context", fake_store):
                        await pair_whatsapp.run(cli_args)

        route = captured["route"]
        assert route.sender_id == "discord:12345"
        assert route.channel == "discord"
        assert route.session_id == "sess-abc"
        assert route.correlation_id == "corr-xyz"
        assert route.reply_to == "12345"


# ---------------------------------------------------------------------------
# check_adapter_health — real aiohttp mock
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCheckAdapterHealth:
    @pytest.mark.asyncio
    async def test_200_is_ok(self, valid_env):
        """HTTP 200 returns (True, 'ok')."""
        mock_mod, _ = _mock_aiohttp(health_status=200)
        env = pair_whatsapp.WhatsAppEnv.from_environ.__func__(pair_whatsapp.WhatsAppEnv)  # noqa: SLF001
        # Use the fixture values directly
        with patch.dict(os.environ, valid_env, clear=True):
            env = pair_whatsapp.WhatsAppEnv.from_environ()
        with patch("aiohttp.ClientSession", mock_mod.ClientSession), \
             patch("aiohttp.ClientTimeout", mock_mod.ClientTimeout):
            ok, detail = await pair_whatsapp.check_adapter_health(env)
        assert ok is True
        assert detail == "ok"

    @pytest.mark.asyncio
    async def test_timeout_is_not_ok(self, valid_env):
        """TimeoutError returns (False, 'timeout')."""
        mock_mod, _ = _mock_aiohttp(health_exc=asyncio.TimeoutError())
        with patch.dict(os.environ, valid_env, clear=True):
            env = pair_whatsapp.WhatsAppEnv.from_environ()
        with patch("aiohttp.ClientSession", mock_mod.ClientSession), \
             patch("aiohttp.ClientTimeout", mock_mod.ClientTimeout):
            ok, detail = await pair_whatsapp.check_adapter_health(env)
        assert ok is False
        assert detail == "timeout"


# ---------------------------------------------------------------------------
# call_gateway_create_connection
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCallGateway:
    @pytest.mark.asyncio
    async def test_200_is_accepted(self, valid_env):
        """HTTP < 400 returns (True, 'accepted')."""
        mock_mod, session_mock = _mock_aiohttp(gw_status=200)
        with patch.dict(os.environ, valid_env, clear=True):
            env = pair_whatsapp.WhatsAppEnv.from_environ()
        with patch("aiohttp.ClientSession", mock_mod.ClientSession), \
             patch("aiohttp.ClientTimeout", mock_mod.ClientTimeout):
            ok, detail = await pair_whatsapp.call_gateway_create_connection(env)
        assert ok is True

    @pytest.mark.asyncio
    async def test_401_is_not_ok(self, valid_env):
        """HTTP 401 returns (False, diagnostic)."""
        mock_mod, _ = _mock_aiohttp(gw_status=401)
        with patch.dict(os.environ, valid_env, clear=True):
            env = pair_whatsapp.WhatsAppEnv.from_environ()
        with patch("aiohttp.ClientSession", mock_mod.ClientSession), \
             patch("aiohttp.ClientTimeout", mock_mod.ClientTimeout):
            ok, detail = await pair_whatsapp.call_gateway_create_connection(env)
        assert ok is False
        assert "401" in detail

    @pytest.mark.asyncio
    async def test_includes_api_key_header(self, valid_env):
        """Request includes x-api-key header."""
        mock_mod, session_mock = _mock_aiohttp(gw_status=200)
        with patch.dict(os.environ, valid_env, clear=True):
            env = pair_whatsapp.WhatsAppEnv.from_environ()
        with patch("aiohttp.ClientSession", mock_mod.ClientSession), \
             patch("aiohttp.ClientTimeout", mock_mod.ClientTimeout):
            await pair_whatsapp.call_gateway_create_connection(env)
        session_mock.post.assert_called_once()
        call_kwargs = session_mock.post.call_args[1]
        assert call_kwargs["headers"]["x-api-key"] == "test-key"
        assert "+33612345678" in session_mock.post.call_args[0][0]
