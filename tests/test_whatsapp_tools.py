"""Unit tests for aiguilleur.channels.whatsapp.tools — the 3 BaseTools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from pathlib import Path

import pytest

from aiguilleur.channels.whatsapp import core as core_module


class TestWhatsAppInstallTool:
    """Verify whatsapp_install tool."""

    def test_tool_metadata(self) -> None:
        from aiguilleur.channels.whatsapp.tools import whatsapp_install

        assert whatsapp_install.name == "whatsapp_install"
        assert "install" in whatsapp_install.description.lower()

    def test_happy_path(self) -> None:
        from aiguilleur.channels.whatsapp.tools import whatsapp_install
        from aiguilleur.channels.whatsapp.core import InstallResult, ApiKeyResult, StepResult

        with patch("aiguilleur.channels.whatsapp.tools.core") as mock_core:
            mock_core.ensure_bun.return_value = (True, "found")
            mock_core.ensure_git.return_value = (True, "found")
            mock_core.resolve_relais_home.return_value = Path("/tmp/.relais")
            mock_core.resolve_project_root.return_value = Path("/tmp")
            mock_core.install_baileys.return_value = InstallResult(ok=True, detail="installed", already_present=False)
            mock_core.generate_api_key.return_value = ApiKeyResult(ok=True, api_key="abc123", detail="generated")
            mock_core.write_env_var.return_value = StepResult(ok=True, detail="set")
            mock_core.enable_channel.return_value = StepResult(ok=True, detail="enabled")
            mock_core.supervisor_ctl.return_value = StepResult(ok=True, detail="started")
            mock_core.MultiStepResult = core_module.MultiStepResult

            result_json = whatsapp_install.invoke({
                "phone_number": "+33600000000",
                "webhook_secret": "mysecret1234567890",
            })

        result = json.loads(result_json)
        assert result["ok"] is True
        assert isinstance(result["steps"], list)
        assert len(result["steps"]) > 0

    def test_bun_missing(self) -> None:
        from aiguilleur.channels.whatsapp.tools import whatsapp_install

        with patch("aiguilleur.channels.whatsapp.tools.core") as mock_core:
            mock_core.ensure_bun.return_value = (False, "bun not found")
            mock_core.resolve_relais_home.return_value = Path("/tmp/.relais")
            mock_core.MultiStepResult = core_module.MultiStepResult

            result_json = whatsapp_install.invoke({
                "phone_number": "+33600000000",
                "webhook_secret": "mysecret1234567890",
            })

        result = json.loads(result_json)
        assert result["ok"] is False


class TestWhatsAppConfigureTool:
    """Verify whatsapp_configure tool dispatches correctly."""

    def test_tool_metadata(self) -> None:
        from aiguilleur.channels.whatsapp.tools import whatsapp_configure

        assert whatsapp_configure.name == "whatsapp_configure"
        assert "action" in whatsapp_configure.description.lower()

    def test_health_action(self) -> None:
        from aiguilleur.channels.whatsapp.tools import whatsapp_configure

        with patch("aiguilleur.channels.whatsapp.tools.asyncio") as mock_asyncio:
            mock_asyncio.run.return_value = MagicMock(ok=True, detail="healthy")

            result_json = whatsapp_configure.invoke({
                "action": "health",
            })

        result = json.loads(result_json)
        assert result["ok"] is True
        assert result["action"] == "health"

    def test_unknown_action(self) -> None:
        from aiguilleur.channels.whatsapp.tools import whatsapp_configure

        result_json = whatsapp_configure.invoke({
            "action": "nonexistent",
        })

        result = json.loads(result_json)
        assert result["ok"] is False
        assert "unknown" in result["detail"].lower()

    def test_enable_action(self) -> None:
        from aiguilleur.channels.whatsapp.tools import whatsapp_configure

        with patch("aiguilleur.channels.whatsapp.tools.core") as mock_core:
            mock_core.resolve_relais_home.return_value = "/tmp/.relais"
            mock_core.resolve_project_root.return_value = "/tmp"
            mock_core.enable_channel.return_value = MagicMock(ok=True, detail="enabled")
            mock_core.supervisor_ctl.return_value = MagicMock(ok=True, detail="restarted")

            result_json = whatsapp_configure.invoke({
                "action": "enable",
            })

        result = json.loads(result_json)
        assert result["ok"] is True
        assert result["action"] == "enable"


class TestWhatsAppUninstallTool:
    """Verify whatsapp_uninstall tool."""

    def test_tool_metadata(self) -> None:
        from aiguilleur.channels.whatsapp.tools import whatsapp_uninstall

        assert whatsapp_uninstall.name == "whatsapp_uninstall"

    def test_basic_uninstall(self) -> None:
        from aiguilleur.channels.whatsapp.tools import whatsapp_uninstall
        from aiguilleur.channels.whatsapp.core import StepResult

        with patch("aiguilleur.channels.whatsapp.tools.core") as mock_core, \
             patch("aiguilleur.channels.whatsapp.tools.asyncio") as mock_asyncio:
            mock_core.resolve_relais_home.return_value = Path("/tmp/.relais")
            mock_core.resolve_project_root.return_value = Path("/tmp")
            mock_core.supervisor_ctl.return_value = StepResult(ok=True, detail="stopped")
            mock_core.disable_channel.return_value = StepResult(ok=True, detail="disabled")
            mock_core.MultiStepResult = core_module.MultiStepResult
            mock_asyncio.run.return_value = StepResult(ok=True, detail="unlinked")

            result_json = whatsapp_uninstall.invoke({})

        result = json.loads(result_json)
        assert result["ok"] is True
        assert isinstance(result["steps"], list)


class TestToolInstances:
    """Verify module-level tool instances exist."""

    def test_all_tools_exported(self) -> None:
        from aiguilleur.channels.whatsapp.tools import whatsapp_install, whatsapp_configure, whatsapp_uninstall

        assert whatsapp_install is not None
        assert whatsapp_configure is not None
        assert whatsapp_uninstall is not None

    def test_tools_have_run_method(self) -> None:
        from aiguilleur.channels.whatsapp.tools import whatsapp_install, whatsapp_configure, whatsapp_uninstall

        for tool in [whatsapp_install, whatsapp_configure, whatsapp_uninstall]:
            assert hasattr(tool, "run")
            assert callable(tool.run)
