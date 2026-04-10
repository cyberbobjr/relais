"""Unit tests for scripts/unpair_whatsapp.py.

Symmetric to tests/test_pair_whatsapp.py.  Covers the unpair / logout
flow invoked by the relais-config subagent via the channel-setup skill.
"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — load the script as a module
# ---------------------------------------------------------------------------

def _load_unpair_script():
    """Import scripts/unpair_whatsapp.py as a module."""
    repo_root = Path(__file__).resolve().parent.parent
    script_path = repo_root / "scripts" / "unpair_whatsapp.py"
    spec = importlib.util.spec_from_file_location("unpair_whatsapp", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["unpair_whatsapp"] = module
    spec.loader.exec_module(module)
    return module


unpair_whatsapp = _load_unpair_script()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def valid_env():
    """Complete set of environment variables for unpair."""
    return {
        "WHATSAPP_GATEWAY_URL": "http://localhost:3025",
        "WHATSAPP_API_KEY": "test-key",
        "WHATSAPP_PHONE_NUMBER": "+33612345678",
    }


@pytest.fixture
def default_args():
    """Default CLI args (no phone-number override)."""
    return MagicMock(phone_number="")


def _async_cm(return_value):
    """Build a mock that behaves as ``async with expr as val:``."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_aiohttp(status=200, body="", exc=None):
    """Build an aiohttp mock for the DELETE call."""
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body)

    session_mock = MagicMock()
    if exc is not None:
        session_mock.delete.side_effect = exc
    else:
        session_mock.delete.return_value = _async_cm(resp)

    mock_mod = MagicMock()
    mock_mod.ClientSession.return_value = _async_cm(session_mock)
    mock_mod.ClientTimeout = MagicMock()
    return mock_mod, session_mock


# ---------------------------------------------------------------------------
# UnpairEnv
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestUnpairEnv:
    def test_from_environ_reads_all_vars(self, valid_env):
        """All env vars loaded correctly."""
        with patch.dict(os.environ, valid_env, clear=True):
            env = unpair_whatsapp.UnpairEnv.from_environ()
        assert env.phone_number == "+33612345678"
        assert env.api_key == "test-key"
        assert env.gateway_url == "http://localhost:3025"

    def test_override_phone_takes_precedence(self, valid_env):
        """CLI override beats env var for phone number."""
        with patch.dict(os.environ, valid_env, clear=True):
            env = unpair_whatsapp.UnpairEnv.from_environ(override_phone="+19999999999")
        assert env.phone_number == "+19999999999"

    def test_override_empty_falls_back_to_env(self, valid_env):
        """Empty override → env var used."""
        with patch.dict(os.environ, valid_env, clear=True):
            env = unpair_whatsapp.UnpairEnv.from_environ(override_phone="")
        assert env.phone_number == "+33612345678"

    def test_gateway_url_trailing_slash_stripped(self):
        """Trailing slash on gateway URL is stripped."""
        with patch.dict(os.environ, {"WHATSAPP_GATEWAY_URL": "http://example.com/"}, clear=True):
            env = unpair_whatsapp.UnpairEnv.from_environ()
        assert env.gateway_url == "http://example.com"


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestParseArgs:
    def test_no_args_ok(self):
        """Parsing with no args sets phone_number to empty string."""
        args = unpair_whatsapp.parse_args([])
        assert args.phone_number == ""

    def test_phone_number_captured(self):
        """--phone-number captured."""
        args = unpair_whatsapp.parse_args(["--phone-number", "+33612345678"])
        assert args.phone_number == "+33612345678"


# ---------------------------------------------------------------------------
# call_gateway_delete_connection
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCallGatewayDelete:
    @pytest.mark.asyncio
    async def test_200_is_logged_out(self, valid_env):
        """HTTP 200 returns (True, 'logged out')."""
        mock_mod, _ = _mock_aiohttp(status=200)
        with patch.dict(os.environ, valid_env, clear=True):
            env = unpair_whatsapp.UnpairEnv.from_environ()
        with patch("aiohttp.ClientSession", mock_mod.ClientSession), \
             patch("aiohttp.ClientTimeout", mock_mod.ClientTimeout):
            ok, detail = await unpair_whatsapp.call_gateway_delete_connection(env)
        assert ok is True
        assert detail == "logged out"

    @pytest.mark.asyncio
    async def test_404_is_treated_as_success(self, valid_env):
        """HTTP 404 → (True, 'already disconnected'). Idempotent."""
        mock_mod, _ = _mock_aiohttp(status=404)
        with patch.dict(os.environ, valid_env, clear=True):
            env = unpair_whatsapp.UnpairEnv.from_environ()
        with patch("aiohttp.ClientSession", mock_mod.ClientSession), \
             patch("aiohttp.ClientTimeout", mock_mod.ClientTimeout):
            ok, detail = await unpair_whatsapp.call_gateway_delete_connection(env)
        assert ok is True
        assert "already" in detail

    @pytest.mark.asyncio
    async def test_401_is_not_ok(self, valid_env):
        """HTTP 401 → (False, diagnostic)."""
        mock_mod, _ = _mock_aiohttp(status=401, body="bad key")
        with patch.dict(os.environ, valid_env, clear=True):
            env = unpair_whatsapp.UnpairEnv.from_environ()
        with patch("aiohttp.ClientSession", mock_mod.ClientSession), \
             patch("aiohttp.ClientTimeout", mock_mod.ClientTimeout):
            ok, detail = await unpair_whatsapp.call_gateway_delete_connection(env)
        assert ok is False
        assert "401" in detail

    @pytest.mark.asyncio
    async def test_500_is_not_ok(self, valid_env):
        """HTTP 500 → (False, diagnostic)."""
        mock_mod, _ = _mock_aiohttp(status=500, body="boom")
        with patch.dict(os.environ, valid_env, clear=True):
            env = unpair_whatsapp.UnpairEnv.from_environ()
        with patch("aiohttp.ClientSession", mock_mod.ClientSession), \
             patch("aiohttp.ClientTimeout", mock_mod.ClientTimeout):
            ok, detail = await unpair_whatsapp.call_gateway_delete_connection(env)
        assert ok is False
        assert "500" in detail

    @pytest.mark.asyncio
    async def test_timeout_is_not_ok(self, valid_env):
        """TimeoutError returns (False, 'timeout contacting gateway')."""
        mock_mod, _ = _mock_aiohttp(exc=asyncio.TimeoutError())
        with patch.dict(os.environ, valid_env, clear=True):
            env = unpair_whatsapp.UnpairEnv.from_environ()
        with patch("aiohttp.ClientSession", mock_mod.ClientSession), \
             patch("aiohttp.ClientTimeout", mock_mod.ClientTimeout):
            ok, detail = await unpair_whatsapp.call_gateway_delete_connection(env)
        assert ok is False
        assert "timeout" in detail.lower()

    @pytest.mark.asyncio
    async def test_includes_api_key_header(self, valid_env):
        """Request includes x-api-key header and correct URL."""
        mock_mod, session_mock = _mock_aiohttp(status=200)
        with patch.dict(os.environ, valid_env, clear=True):
            env = unpair_whatsapp.UnpairEnv.from_environ()
        with patch("aiohttp.ClientSession", mock_mod.ClientSession), \
             patch("aiohttp.ClientTimeout", mock_mod.ClientTimeout):
            await unpair_whatsapp.call_gateway_delete_connection(env)
        session_mock.delete.assert_called_once()
        call_args = session_mock.delete.call_args
        assert "+33612345678" in call_args[0][0]
        assert call_args[1]["headers"]["x-api-key"] == "test-key"


# ---------------------------------------------------------------------------
# run() — integration of the full flow
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRun:
    @pytest.mark.asyncio
    async def test_missing_phone_number_returns_bad_args(self, default_args):
        """No phone number anywhere → EXIT_BAD_ARGS."""
        with patch.dict(os.environ, {"WHATSAPP_API_KEY": "k"}, clear=True):
            code = await unpair_whatsapp.run(default_args)
        assert code == unpair_whatsapp.EXIT_BAD_ARGS

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_bad_args(self, default_args):
        """Empty WHATSAPP_API_KEY → EXIT_BAD_ARGS."""
        with patch.dict(os.environ, {"WHATSAPP_PHONE_NUMBER": "+33600000000"}, clear=True):
            code = await unpair_whatsapp.run(default_args)
        assert code == unpair_whatsapp.EXIT_BAD_ARGS

    @pytest.mark.asyncio
    async def test_gateway_failure_returns_gateway_failed(self, default_args, valid_env):
        """Gateway DELETE returns 500 → EXIT_GATEWAY_FAILED."""
        with patch.dict(os.environ, valid_env, clear=True):
            with patch.object(
                unpair_whatsapp,
                "call_gateway_delete_connection",
                AsyncMock(return_value=(False, "HTTP 500: boom")),
            ):
                code = await unpair_whatsapp.run(default_args)
        assert code == unpair_whatsapp.EXIT_GATEWAY_FAILED

    @pytest.mark.asyncio
    async def test_redis_failure_returns_redis_failed(self, default_args, valid_env):
        """Redis delete failure → EXIT_REDIS_FAILED."""
        with patch.dict(os.environ, valid_env, clear=True):
            with patch.object(
                unpair_whatsapp,
                "call_gateway_delete_connection",
                AsyncMock(return_value=(True, "logged out")),
            ):
                with patch.object(
                    unpair_whatsapp,
                    "delete_pairing_context",
                    AsyncMock(return_value=(False, "ConnectionError")),
                ):
                    code = await unpair_whatsapp.run(default_args)
        assert code == unpair_whatsapp.EXIT_REDIS_FAILED

    @pytest.mark.asyncio
    async def test_happy_path_returns_ok(self, default_args, valid_env):
        """All steps succeed → EXIT_OK."""
        with patch.dict(os.environ, valid_env, clear=True):
            with patch.object(
                unpair_whatsapp,
                "call_gateway_delete_connection",
                AsyncMock(return_value=(True, "logged out")),
            ):
                with patch.object(
                    unpair_whatsapp,
                    "delete_pairing_context",
                    AsyncMock(return_value=(True, "deleted (1 key(s))")),
                ):
                    code = await unpair_whatsapp.run(default_args)
        assert code == unpair_whatsapp.EXIT_OK

    @pytest.mark.asyncio
    async def test_already_disconnected_is_ok(self, default_args, valid_env):
        """HTTP 404 from gateway still succeeds (idempotent)."""
        with patch.dict(os.environ, valid_env, clear=True):
            with patch.object(
                unpair_whatsapp,
                "call_gateway_delete_connection",
                AsyncMock(return_value=(True, "already disconnected")),
            ):
                with patch.object(
                    unpair_whatsapp,
                    "delete_pairing_context",
                    AsyncMock(return_value=(True, "deleted (0 key(s))")),
                ):
                    code = await unpair_whatsapp.run(default_args)
        assert code == unpair_whatsapp.EXIT_OK

    @pytest.mark.asyncio
    async def test_cli_phone_override_used(self, valid_env):
        """--phone-number on CLI overrides WHATSAPP_PHONE_NUMBER env."""
        captured = {}

        async def fake_delete(env):
            captured["phone"] = env.phone_number
            return True, "logged out"

        args = MagicMock(phone_number="+19999999999")
        with patch.dict(os.environ, valid_env, clear=True):
            with patch.object(unpair_whatsapp, "call_gateway_delete_connection", fake_delete):
                with patch.object(
                    unpair_whatsapp,
                    "delete_pairing_context",
                    AsyncMock(return_value=(True, "deleted (0 key(s))")),
                ):
                    await unpair_whatsapp.run(args)

        assert captured["phone"] == "+19999999999"
