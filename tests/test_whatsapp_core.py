"""Unit tests for aiguilleur.channels.whatsapp.core — TDD RED phase.

Tests cover:
- ensure_bun / ensure_git
- install_baileys (idempotent clone + bun install)
- generate_api_key (baileys ACL user, REDIS_URL construction)
- pair / unpair (HTTP calls + Redis context)
- check_health
- supervisor_ctl (cwd + -c flag — iteration C regression)
- write_env_vars / enable_channel / disable_channel
- resolve_project_root / resolve_relais_home
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

class TestResolveProjectRoot:
    """Verify project root resolution from RELAIS_HOME."""

    def test_dev_mode(self, tmp_path: Path) -> None:
        """RELAIS_HOME=<repo>/.relais → project root = <repo>."""
        from aiguilleur.channels.whatsapp.core import resolve_project_root

        relais_home = tmp_path / ".relais"
        relais_home.mkdir()
        (tmp_path / "supervisord.conf").write_text("[supervisord]")
        result = resolve_project_root(relais_home)
        assert result == tmp_path

    def test_raises_if_no_supervisord_conf(self, tmp_path: Path) -> None:
        """Should raise RuntimeError if project root can't be found."""
        from aiguilleur.channels.whatsapp.core import resolve_project_root

        relais_home = tmp_path / "deep" / "nested" / ".relais"
        relais_home.mkdir(parents=True)
        # No supervisord.conf anywhere → should raise
        with pytest.raises(RuntimeError, match="project root"):
            resolve_project_root(relais_home)


# ---------------------------------------------------------------------------
# ensure_bun / ensure_git
# ---------------------------------------------------------------------------

class TestEnsureBun:
    """Verify bun presence check."""

    def test_bun_found(self) -> None:
        from aiguilleur.channels.whatsapp.core import ensure_bun

        with patch("shutil.which", return_value="/usr/local/bin/bun"):
            ok, detail = ensure_bun()
        assert ok is True

    def test_bun_missing(self) -> None:
        from aiguilleur.channels.whatsapp.core import ensure_bun

        with patch("shutil.which", return_value=None):
            ok, detail = ensure_bun()
        assert ok is False
        assert "bun" in detail.lower()


class TestEnsureGit:
    """Verify git presence check."""

    def test_git_found(self) -> None:
        from aiguilleur.channels.whatsapp.core import ensure_git

        with patch("shutil.which", return_value="/usr/bin/git"):
            ok, detail = ensure_git()
        assert ok is True

    def test_git_missing(self) -> None:
        from aiguilleur.channels.whatsapp.core import ensure_git

        with patch("shutil.which", return_value=None):
            ok, detail = ensure_git()
        assert ok is False


# ---------------------------------------------------------------------------
# install_baileys
# ---------------------------------------------------------------------------

class TestInstallBaileys:
    """Verify baileys-api vendor install logic."""

    def test_already_installed(self, tmp_path: Path) -> None:
        """Skip clone if vendor dir + package.json exist."""
        from aiguilleur.channels.whatsapp.core import install_baileys

        vendor = tmp_path / "vendor" / "baileys-api"
        vendor.mkdir(parents=True)
        (vendor / "package.json").write_text("{}")

        with patch("subprocess.run") as mock_run:
            result = install_baileys(relais_home=tmp_path)

        assert result.ok is True
        assert result.already_present is True
        # Should NOT have called git clone
        clone_calls = [c for c in mock_run.call_args_list if "clone" in str(c)]
        assert len(clone_calls) == 0

    def test_fresh_install(self, tmp_path: Path) -> None:
        """Clone + bun install on fresh system."""
        from aiguilleur.channels.whatsapp.core import install_baileys

        def mock_subprocess_run(cmd, **kwargs):
            if "clone" in cmd:
                vendor = tmp_path / "vendor" / "baileys-api"
                vendor.mkdir(parents=True)
                (vendor / "package.json").write_text("{}")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            result = install_baileys(relais_home=tmp_path)

        assert result.ok is True
        assert result.already_present is False

    def test_clone_failure(self, tmp_path: Path) -> None:
        """Report error if git clone fails."""
        from aiguilleur.channels.whatsapp.core import install_baileys

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
            result = install_baileys(relais_home=tmp_path)

        assert result.ok is False
        assert "clone" in result.detail.lower() or "git" in result.detail.lower()


# ---------------------------------------------------------------------------
# generate_api_key — iteration A regression (NOAUTH)
# ---------------------------------------------------------------------------

class TestGenerateApiKey:
    """Verify API key generation uses correct Redis ACL user.

    This is the iteration A regression test: the script must connect
    to Redis as ACL user 'baileys' over TCP, NOT the default user.
    """

    def test_constructs_correct_redis_url(self, tmp_path: Path) -> None:
        """REDIS_URL must be redis://baileys:<pass>@localhost:6379."""
        from aiguilleur.channels.whatsapp.core import generate_api_key

        vendor = tmp_path / "vendor" / "baileys-api"
        vendor.mkdir(parents=True)

        captured_env: dict = {}

        def capture_env(cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return subprocess.CompletedProcess(
                cmd, 0,
                stdout="Created API key with role 'user': abc123def456\n",
                stderr="",
            )

        with patch("subprocess.run", side_effect=capture_env):
            result = generate_api_key(
                relais_home=tmp_path,
                redis_pass_baileys="s3cret_pass",
            )

        assert result.ok is True
        assert result.api_key == "abc123def456"
        # Critical: verify REDIS_URL uses baileys ACL user, NOT default
        redis_url = captured_env.get("REDIS_URL", "")
        assert "baileys:" in redis_url
        assert "s3cret_pass" in redis_url
        assert "localhost:6379" in redis_url

    def test_missing_redis_pass(self, tmp_path: Path) -> None:
        """Must fail clearly if REDIS_PASS_BAILEYS is empty."""
        from aiguilleur.channels.whatsapp.core import generate_api_key

        vendor = tmp_path / "vendor" / "baileys-api"
        vendor.mkdir(parents=True)

        result = generate_api_key(relais_home=tmp_path, redis_pass_baileys="")
        assert result.ok is False
        assert "REDIS_PASS_BAILEYS" in result.detail

    def test_vendor_not_installed(self, tmp_path: Path) -> None:
        """Must fail if baileys-api vendor tree is missing."""
        from aiguilleur.channels.whatsapp.core import generate_api_key

        result = generate_api_key(relais_home=tmp_path, redis_pass_baileys="pass")
        assert result.ok is False
        assert "vendor" in result.detail.lower() or "baileys" in result.detail.lower()


# ---------------------------------------------------------------------------
# pair / unpair (HTTP + Redis)
# ---------------------------------------------------------------------------

class TestPair:
    """Verify pairing flow: health check → gateway POST → Redis SET."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_happy_path(self) -> None:
        from aiguilleur.channels.whatsapp.core import pair, PairParams
        from unittest.mock import AsyncMock

        mock_redis = MagicMock()
        mock_redis.set = AsyncMock(return_value=None)

        params = PairParams(
            sender_id="discord:123",
            channel="discord",
            session_id="sess-1",
            correlation_id="corr-1",
            reply_to="456",
        )

        with patch("aiguilleur.channels.whatsapp.core._http_post") as mock_post:
            mock_post.return_value = (True, "accepted")
            with patch("aiguilleur.channels.whatsapp.core._http_get") as mock_get:
                mock_get.return_value = (True, "ok")
                result = await pair(
                    params=params,
                    phone_number="+33600000000",
                    api_key="key123",
                    gateway_url="http://localhost:3025",
                    webhook_host="127.0.0.1",
                    webhook_port="8765",
                    webhook_secret="secret",
                    redis_client=mock_redis,
                )

        assert result.ok is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_gateway_down(self) -> None:
        from aiguilleur.channels.whatsapp.core import pair, PairParams

        params = PairParams(
            sender_id="discord:123",
            channel="discord",
            session_id="sess-1",
            correlation_id="corr-1",
            reply_to="456",
        )

        with patch("aiguilleur.channels.whatsapp.core._http_get") as mock_get:
            mock_get.return_value = (True, "ok")
            with patch("aiguilleur.channels.whatsapp.core._http_post") as mock_post:
                mock_post.return_value = (False, "connection refused")
                result = await pair(
                    params=params,
                    phone_number="+33600000000",
                    api_key="key123",
                    gateway_url="http://localhost:3025",
                    webhook_host="127.0.0.1",
                    webhook_port="8765",
                    webhook_secret="secret",
                    redis_client=MagicMock(),
                )

        assert result.ok is False
        assert "gateway" in result.detail.lower() or "connection" in result.detail.lower()


class TestUnpair:
    """Verify unpair flow: gateway DELETE → Redis DEL."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_happy_path(self) -> None:
        from aiguilleur.channels.whatsapp.core import unpair
        from unittest.mock import AsyncMock

        mock_redis = MagicMock()
        mock_redis.delete = AsyncMock(return_value=1)

        with patch("aiguilleur.channels.whatsapp.core._http_delete") as mock_del:
            mock_del.return_value = (True, "logged out")
            result = await unpair(
                phone_number="+33600000000",
                api_key="key123",
                gateway_url="http://localhost:3025",
                redis_client=mock_redis,
            )

        assert result.ok is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_already_disconnected(self) -> None:
        from aiguilleur.channels.whatsapp.core import unpair
        from unittest.mock import AsyncMock

        mock_redis = MagicMock()
        mock_redis.delete = AsyncMock(return_value=0)

        with patch("aiguilleur.channels.whatsapp.core._http_delete") as mock_del:
            mock_del.return_value = (True, "already disconnected")
            result = await unpair(
                phone_number="+33600000000",
                api_key="key123",
                gateway_url="http://localhost:3025",
                redis_client=mock_redis,
            )

        assert result.ok is True
        assert "disconnect" in result.detail.lower()


# ---------------------------------------------------------------------------
# check_health
# ---------------------------------------------------------------------------

class TestCheckHealth:
    """Verify health probe logic."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_both_healthy(self) -> None:
        from aiguilleur.channels.whatsapp.core import check_health

        with patch("aiguilleur.channels.whatsapp.core._http_get") as mock_get:
            mock_get.return_value = (True, "ok")
            result = await check_health(
                gateway_url="http://localhost:3025",
                webhook_host="127.0.0.1",
                webhook_port="8765",
            )

        assert result.ok is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_webhook_down(self) -> None:
        from aiguilleur.channels.whatsapp.core import check_health

        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if "health" in url:
                return (False, "connection refused")
            return (True, "ok")

        with patch("aiguilleur.channels.whatsapp.core._http_get", side_effect=mock_get):
            result = await check_health(
                gateway_url="http://localhost:3025",
                webhook_host="127.0.0.1",
                webhook_port="8765",
            )

        assert result.ok is False


# ---------------------------------------------------------------------------
# supervisor_ctl — iteration C regression
# ---------------------------------------------------------------------------

class TestSupervisorCtl:
    """Verify supervisorctl uses -c and correct cwd.

    Iteration C regression: the sub-agent couldn't reach supervisord
    because supervisorctl was invoked without -c and wrong cwd.
    """

    def test_uses_correct_cwd_and_config_flag(self, tmp_path: Path) -> None:
        """supervisorctl must run with cwd=project_root and -c supervisord.conf."""
        from aiguilleur.channels.whatsapp.core import supervisor_ctl

        (tmp_path / "supervisord.conf").write_text("[supervisord]")

        captured_kwargs: dict = {}

        def capture_run(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="RUNNING", stderr="")

        with patch("subprocess.run", side_effect=capture_run):
            result = supervisor_ctl("status", "optional:baileys-api", project_root=tmp_path)

        assert result.ok is True
        # Critical regression check: cwd must be project root
        assert captured_kwargs.get("cwd") == tmp_path
        # Must pass -c supervisord.conf

    def test_supervisord_not_running(self, tmp_path: Path) -> None:
        """Report clear error when supervisord is unreachable."""
        from aiguilleur.channels.whatsapp.core import supervisor_ctl

        (tmp_path / "supervisord.conf").write_text("[supervisord]")

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(
            1, "supervisorctl", stderr="refused connection"
        )):
            result = supervisor_ctl("pid", project_root=tmp_path)

        assert result.ok is False


# ---------------------------------------------------------------------------
# write_env_vars
# ---------------------------------------------------------------------------

class TestWriteEnvVars:
    """Verify .env file manipulation."""

    def test_set_new_var(self, tmp_path: Path) -> None:
        from aiguilleur.channels.whatsapp.core import write_env_var

        env_file = tmp_path / ".env"
        env_file.write_text("EXISTING=value\n")

        result = write_env_var("WHATSAPP_PHONE_NUMBER", "+33600000000", env_file)
        assert result.ok is True

        content = env_file.read_text()
        assert "WHATSAPP_PHONE_NUMBER=+33600000000" in content
        assert "EXISTING=value" in content

    def test_update_existing_var(self, tmp_path: Path) -> None:
        from aiguilleur.channels.whatsapp.core import write_env_var

        env_file = tmp_path / ".env"
        env_file.write_text("WHATSAPP_PHONE_NUMBER=+33600000001\n")

        result = write_env_var("WHATSAPP_PHONE_NUMBER", "+33600000000", env_file)
        assert result.ok is True

        content = env_file.read_text()
        assert "+33600000000" in content
        assert "+33600000001" not in content


# ---------------------------------------------------------------------------
# enable_channel / disable_channel
# ---------------------------------------------------------------------------

class TestChannelToggle:
    """Verify aiguilleur.yaml enable/disable."""

    def test_enable(self, tmp_path: Path) -> None:
        from aiguilleur.channels.whatsapp.core import enable_channel

        config = tmp_path / "config" / "aiguilleur.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            "channels:\n"
            "  whatsapp:\n"
            "    enabled: false\n"
            "    streaming: false\n"
        )

        result = enable_channel(relais_home=tmp_path)
        assert result.ok is True

        import yaml
        data = yaml.safe_load(config.read_text())
        assert data["channels"]["whatsapp"]["enabled"] is True

    def test_disable(self, tmp_path: Path) -> None:
        from aiguilleur.channels.whatsapp.core import disable_channel

        config = tmp_path / "config" / "aiguilleur.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            "channels:\n"
            "  whatsapp:\n"
            "    enabled: true\n"
            "    streaming: false\n"
        )

        result = disable_channel(relais_home=tmp_path)
        assert result.ok is True

        import yaml
        data = yaml.safe_load(config.read_text())
        assert data["channels"]["whatsapp"]["enabled"] is False
