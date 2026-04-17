"""WhatsApp channel pack — 3 LangChain BaseTools.

Tools:
- ``whatsapp_install``   — one-call full install
- ``whatsapp_configure`` — action-based configure/pair/unpair/health/status
- ``whatsapp_uninstall`` — reverse of install
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from aiguilleur.channels.whatsapp import core


# ---------------------------------------------------------------------------
# Security — allowlist for set_env key names
# ---------------------------------------------------------------------------

# Only these environment variable names may be written by the set_env action.
# This prevents an LLM-controlled caller from overwriting arbitrary env vars
# (e.g. REDIS_PASSWORD, ANTHROPIC_API_KEY, PATH, etc.).
_ALLOWED_ENV_KEYS: frozenset[str] = frozenset({
    "WHATSAPP_PHONE_NUMBER",
    "WHATSAPP_API_KEY",
    "WHATSAPP_WEBHOOK_SECRET",
    "WHATSAPP_GATEWAY_URL",
    "WHATSAPP_WEBHOOK_HOST",
    "WHATSAPP_WEBHOOK_PORT",
    "REDIS_PASS_BAILEYS",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_env() -> dict[str, str]:
    """Read WhatsApp env vars from the current process environment."""
    return {
        "phone_number": os.environ.get("WHATSAPP_PHONE_NUMBER", ""),
        "api_key": os.environ.get("WHATSAPP_API_KEY", ""),
        "gateway_url": os.environ.get("WHATSAPP_GATEWAY_URL", "http://localhost:3025"),
        "webhook_host": os.environ.get("WHATSAPP_WEBHOOK_HOST", "127.0.0.1"),
        "webhook_port": os.environ.get("WHATSAPP_WEBHOOK_PORT", "8765"),
        "webhook_secret": os.environ.get("WHATSAPP_WEBHOOK_SECRET", ""),
        "redis_pass_baileys": os.environ.get("REDIS_PASS_BAILEYS", ""),
    }


def _json(data: dict) -> str:
    """Serialize a dict to JSON string for LLM consumption."""
    return json.dumps(data, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 1: whatsapp_install
# ---------------------------------------------------------------------------

class WhatsAppInstallTool(BaseTool):
    """Install the WhatsApp channel end-to-end.

    One call handles: vendor clone, API key generation, env vars,
    channel enable, and service start.
    """

    name: str = "whatsapp_install"
    description: str = (
        "Install the WhatsApp channel: clones baileys-api vendor, "
        "generates an API key, writes env vars, enables the channel "
        "in aiguilleur.yaml, and starts services. Idempotent. "
        "Params: phone_number (str, E.164), webhook_secret (str, min 16 chars)."
    )

    def _run(self, phone_number: str = "", webhook_secret: str = "", **kwargs: Any) -> str:
        """Execute the full install pipeline."""
        result = core.MultiStepResult()
        relais_home = Path(str(core.resolve_relais_home()))

        # 1. Prerequisites
        ok, detail = core.ensure_bun()
        result.add("ensure_bun", "done" if ok else "failed", detail)
        if not ok:
            return _json({"ok": False, "steps": result.steps})

        ok, detail = core.ensure_git()
        result.add("ensure_git", "done" if ok else "failed", detail)
        if not ok:
            return _json({"ok": False, "steps": result.steps})

        # 2. Install vendor
        install_result = core.install_baileys(relais_home=relais_home)
        status = "skipped" if install_result.already_present else ("done" if install_result.ok else "failed")
        result.add("install_baileys", status, install_result.detail)
        if not install_result.ok:
            return _json({"ok": False, "steps": result.steps})

        # 3. Generate API key
        redis_pass = os.environ.get("REDIS_PASS_BAILEYS", "")
        key_result = core.generate_api_key(
            relais_home=relais_home,
            redis_pass_baileys=redis_pass,
        )
        result.add("generate_api_key", "done" if key_result.ok else "failed", key_result.detail)
        if not key_result.ok:
            return _json({"ok": False, "steps": result.steps})

        # 4. Write env vars
        project_root = Path(str(core.resolve_project_root(relais_home)))
        env_file = project_root / ".env"

        for key, value in [
            ("WHATSAPP_PHONE_NUMBER", phone_number),
            ("WHATSAPP_API_KEY", key_result.api_key),
            ("WHATSAPP_WEBHOOK_SECRET", webhook_secret),
        ]:
            r = core.write_env_var(key, value, env_file)
            result.add(f"write_{key}", "done" if r.ok else "failed", "set")
            if not r.ok:
                return _json({"ok": False, "steps": result.steps})

        # 5. Enable channel
        r = core.enable_channel(relais_home)
        result.add("enable_channel", "done" if r.ok else "failed", r.detail)

        # 6. Start services
        for svc in ["optional:baileys-api"]:
            r = core.supervisor_ctl("start", svc, project_root=project_root)
            result.add(f"start_{svc}", "done" if r.ok else "failed", r.detail)

        r = core.supervisor_ctl("restart", "aiguilleur", project_root=project_root)
        result.add("restart_aiguilleur", "done" if r.ok else "failed", r.detail)

        return _json({"ok": result.ok, "steps": result.steps})


# ---------------------------------------------------------------------------
# Tool 2: whatsapp_configure
# ---------------------------------------------------------------------------

class WhatsAppConfigureTool(BaseTool):
    """Configure the WhatsApp channel.

    Uses an ``action`` parameter to dispatch: pair, unpair, health,
    status, enable, disable, set_env.
    """

    name: str = "whatsapp_configure"
    description: str = (
        "Configure the WhatsApp channel. Use the 'action' parameter to select: "
        "pair, unpair, health, status, enable, disable, set_env. "
        "For pair: pass params={sender_id, channel, session_id, correlation_id, reply_to}. "
        "For set_env: pass params={key, value}."
    )

    def _run(self, action: str = "", params: dict | None = None, **kwargs: Any) -> str:
        """Dispatch to the appropriate core function."""
        params = params or {}
        relais_home = Path(str(core.resolve_relais_home()))

        if action == "health":
            env = _load_env()
            r = asyncio.run(core.check_health(
                gateway_url=env["gateway_url"],
                webhook_host=env["webhook_host"],
                webhook_port=env["webhook_port"],
            ))
            return _json({"ok": r.ok, "action": "health", "detail": r.detail})

        if action == "status":
            project_root = Path(str(core.resolve_project_root(relais_home)))
            r_baileys = core.supervisor_ctl("status", "optional:baileys-api", project_root=project_root)
            r_aiguilleur = core.supervisor_ctl("status", "aiguilleur", project_root=project_root)
            return _json({
                "ok": True,
                "action": "status",
                "detail": f"baileys-api: {r_baileys.detail}; aiguilleur: {r_aiguilleur.detail}",
            })

        if action == "pair":
            env = _load_env()
            pair_params = core.PairParams(
                sender_id=params.get("sender_id", ""),
                channel=params.get("channel", ""),
                session_id=params.get("session_id", ""),
                correlation_id=params.get("correlation_id", ""),
                reply_to=params.get("reply_to", ""),
            )
            # Get redis client
            from common.redis_client import RedisClient
            async def _pair() -> core.StepResult:
                client = RedisClient("commandant")
                conn = await client.get_connection()
                try:
                    return await core.pair(
                        params=pair_params,
                        phone_number=env["phone_number"],
                        api_key=env["api_key"],
                        gateway_url=env["gateway_url"],
                        webhook_host=env["webhook_host"],
                        webhook_port=env["webhook_port"],
                        webhook_secret=env["webhook_secret"],
                        redis_client=conn,
                    )
                finally:
                    await client.close()

            r = asyncio.run(_pair())
            return _json({"ok": r.ok, "action": "pair", "detail": r.detail})

        if action == "unpair":
            env = _load_env()
            phone = params.get("phone_number", env["phone_number"])

            from common.redis_client import RedisClient
            async def _unpair() -> core.StepResult:
                client = RedisClient("commandant")
                conn = await client.get_connection()
                try:
                    return await core.unpair(
                        phone_number=phone,
                        api_key=env["api_key"],
                        gateway_url=env["gateway_url"],
                        redis_client=conn,
                    )
                finally:
                    await client.close()

            r = asyncio.run(_unpair())
            return _json({"ok": r.ok, "action": "unpair", "detail": r.detail})

        if action == "enable":
            r = core.enable_channel(relais_home)
            if r.ok:
                project_root = Path(str(core.resolve_project_root(relais_home)))
                core.supervisor_ctl("restart", "aiguilleur", project_root=project_root)
            return _json({"ok": r.ok, "action": "enable", "detail": r.detail})

        if action == "disable":
            r = core.disable_channel(relais_home)
            if r.ok:
                project_root = Path(str(core.resolve_project_root(relais_home)))
                core.supervisor_ctl("restart", "aiguilleur", project_root=project_root)
            return _json({"ok": r.ok, "action": "disable", "detail": r.detail})

        if action == "set_env":
            project_root = Path(str(core.resolve_project_root(relais_home)))
            env_file = project_root / ".env"
            key = params.get("key", "")
            value = params.get("value", "")
            if not key:
                return _json({"ok": False, "action": "set_env", "detail": "missing 'key' in params"})
            if key not in _ALLOWED_ENV_KEYS:
                return _json({
                    "ok": False,
                    "action": "set_env",
                    "detail": f"key '{key}' is not allowed; permitted keys: {sorted(_ALLOWED_ENV_KEYS)}",
                })
            r = core.write_env_var(key, value, env_file)
            return _json({"ok": r.ok, "action": "set_env", "detail": r.detail})

        return _json({"ok": False, "action": action, "detail": f"Unknown action '{action}'. Valid: pair, unpair, health, status, enable, disable, set_env"})


# ---------------------------------------------------------------------------
# Tool 3: whatsapp_uninstall
# ---------------------------------------------------------------------------

class WhatsAppUninstallTool(BaseTool):
    """Uninstall the WhatsApp channel.

    Unpairs, stops services, disables channel. Optionally cleans
    vendor tree and .env credentials.
    """

    name: str = "whatsapp_uninstall"
    description: str = (
        "Uninstall WhatsApp: unpairs, stops services, disables channel. "
        "Optional: clean_vendor (bool, remove vendor tree), "
        "clean_env (bool, remove WHATSAPP_* from .env)."
    )

    def _run(self, clean_vendor: bool = False, clean_env: bool = False, **kwargs: Any) -> str:
        """Execute the uninstall pipeline."""
        result = core.MultiStepResult()
        relais_home = Path(str(core.resolve_relais_home()))
        project_root = Path(str(core.resolve_project_root(relais_home)))
        env = _load_env()

        # 1. Unpair (best-effort)
        try:
            from common.redis_client import RedisClient
            async def _unpair() -> core.StepResult:
                client = RedisClient("commandant")
                conn = await client.get_connection()
                try:
                    return await core.unpair(
                        phone_number=env["phone_number"],
                        api_key=env["api_key"],
                        gateway_url=env["gateway_url"],
                        redis_client=conn,
                    )
                finally:
                    await client.close()

            r = asyncio.run(_unpair())
            result.add("unpair", "done" if r.ok else "failed", r.detail)
        except Exception as exc:
            result.add("unpair", "failed", str(exc))

        # 2. Stop services
        r = core.supervisor_ctl("stop", "optional:baileys-api", project_root=project_root)
        result.add("stop_baileys", "done" if r.ok else "failed", r.detail)

        # 3. Disable channel
        r = core.disable_channel(relais_home)
        result.add("disable_channel", "done" if r.ok else "failed", r.detail)

        r = core.supervisor_ctl("restart", "aiguilleur", project_root=project_root)
        result.add("restart_aiguilleur", "done" if r.ok else "failed", r.detail)

        # 4. Optional cleanup
        if clean_vendor:
            import shutil
            vendor = relais_home / "vendor" / "baileys-api"
            if vendor.is_dir():
                shutil.rmtree(vendor)
                result.add("clean_vendor", "done", "vendor tree removed")
            else:
                result.add("clean_vendor", "skipped", "vendor not present")

        if clean_env:
            env_file = project_root / ".env"
            for key in ["WHATSAPP_PHONE_NUMBER", "WHATSAPP_API_KEY", "WHATSAPP_WEBHOOK_SECRET"]:
                core.write_env_var(key, "", env_file)
            result.add("clean_env", "done", "WHATSAPP_* vars cleared")

        return _json({"ok": result.ok, "steps": result.steps})


# ---------------------------------------------------------------------------
# Module-level instances (collected by SubagentRegistry)
# ---------------------------------------------------------------------------

whatsapp_install = WhatsAppInstallTool()
whatsapp_configure = WhatsAppConfigureTool()
whatsapp_uninstall = WhatsAppUninstallTool()
