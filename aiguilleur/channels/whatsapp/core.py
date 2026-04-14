"""WhatsApp channel pack — all business logic.

Consolidates install, pair, unpair, health, supervisor control, env
management, and channel config toggling.  Every function is a pure
library call (no sys.exit, no argparse, no print).  CLI and BaseTools
are thin wrappers over this module.

Key invariants encoded here (previously lived in SKILL.md prose):
- baileys-api connects to Redis over TCP as ACL user ``baileys``
- supervisorctl requires ``-c supervisord.conf`` with cwd=project_root
- Runtime configs live at ``$RELAIS_HOME/config/``, not ``./config/``
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("relais.aiguilleur.channels.whatsapp")


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StepResult:
    """Result of a single operation."""

    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class InstallResult:
    """Result of install_baileys."""

    ok: bool
    detail: str = ""
    already_present: bool = False
    vendor_path: str = ""


@dataclass(frozen=True)
class ApiKeyResult:
    """Result of generate_api_key."""

    ok: bool
    detail: str = ""
    api_key: str = ""


@dataclass(frozen=True)
class PairParams:
    """Routing context for the pairing flow."""

    sender_id: str
    channel: str
    session_id: str
    correlation_id: str
    reply_to: str


@dataclass
class MultiStepResult:
    """Result with per-step breakdown."""

    ok: bool = True
    steps: list[dict[str, str]] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        """Record one step."""
        self.steps.append({"name": name, "status": status, "detail": detail})
        if status == "failed":
            self.ok = False


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def resolve_relais_home() -> Path:
    """Resolve RELAIS_HOME from environment or default."""
    raw = os.environ.get("RELAIS_HOME", "")
    if raw:
        return Path(raw)
    # Dev mode fallback: look for .relais in project root
    cwd = Path.cwd()
    candidate = cwd / ".relais"
    if candidate.is_dir():
        return candidate
    return Path.home() / ".relais"


def resolve_project_root(relais_home: Path | None = None) -> Path:
    """Derive the project root from RELAIS_HOME.

    In dev mode, RELAIS_HOME is ``<repo>/.relais`` → parent is the
    project root.  We verify by checking for ``supervisord.conf``.

    Args:
        relais_home: Explicit RELAIS_HOME path.  If None, resolved
            from the environment.

    Returns:
        The project root directory.

    Raises:
        RuntimeError: If the project root cannot be determined.
    """
    if relais_home is None:
        relais_home = resolve_relais_home()

    candidate = relais_home.parent
    if (candidate / "supervisord.conf").is_file():
        return candidate

    # Walk up looking for supervisord.conf
    for parent in relais_home.parents:
        if (parent / "supervisord.conf").is_file():
            return parent

    # Check env override
    env_root = os.environ.get("RELAIS_PROJECT_ROOT", "")
    if env_root and (Path(env_root) / "supervisord.conf").is_file():
        return Path(env_root)

    raise RuntimeError(
        f"Cannot determine project root from RELAIS_HOME={relais_home}. "
        "Set RELAIS_PROJECT_ROOT or ensure supervisord.conf exists in "
        "the parent directory."
    )


# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------

def ensure_bun() -> tuple[bool, str]:
    """Check that the bun runtime is available on PATH."""
    if shutil.which("bun"):
        return True, "bun found"
    return False, "bun not found. Install: curl -fsSL https://bun.sh/install | bash"


def ensure_git() -> tuple[bool, str]:
    """Check that git is available on PATH."""
    if shutil.which("git"):
        return True, "git found"
    return False, "git not found"


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def install_baileys(
    relais_home: Path,
    pinned_sha: str = "main",
) -> InstallResult:
    """Clone and install baileys-api into the vendor directory.

    Idempotent: skips clone if directory + package.json already exist.

    Args:
        relais_home: RELAIS_HOME directory.
        pinned_sha: Git ref to checkout (default: main).

    Returns:
        InstallResult with vendor_path and status.
    """
    vendor_dir = relais_home / "vendor" / "baileys-api"

    if vendor_dir.is_dir() and (vendor_dir / "package.json").is_file():
        return InstallResult(
            ok=True,
            detail="baileys-api already installed",
            already_present=True,
            vendor_path=str(vendor_dir),
        )

    # Clone
    try:
        vendor_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "https://github.com/fazer-ai/baileys-api.git", str(vendor_dir)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if pinned_sha != "main":
            subprocess.run(
                ["git", "checkout", pinned_sha],
                cwd=vendor_dir,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
    except subprocess.CalledProcessError as exc:
        return InstallResult(
            ok=False,
            detail=f"git clone failed: {exc.stderr or exc.stdout or str(exc)}",
        )
    except subprocess.TimeoutExpired:
        return InstallResult(ok=False, detail="git clone timed out")

    # bun install
    try:
        subprocess.run(
            ["bun", "install"],
            cwd=vendor_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        return InstallResult(
            ok=False,
            detail=f"bun install failed: {exc.stderr or str(exc)}",
        )
    except subprocess.TimeoutExpired:
        return InstallResult(ok=False, detail="bun install timed out")

    return InstallResult(
        ok=True,
        detail="baileys-api installed successfully",
        already_present=False,
        vendor_path=str(vendor_dir),
    )


# ---------------------------------------------------------------------------
# API key generation — encapsulates iteration A (NOAUTH) fix
# ---------------------------------------------------------------------------

def generate_api_key(
    relais_home: Path,
    redis_pass_baileys: str,
    role: str = "user",
    label: str = "relais-adapter",
) -> ApiKeyResult:
    """Generate a baileys-api key via manage-api-keys.ts.

    Constructs ``REDIS_URL=redis://baileys:<pass>@localhost:6379``
    so the script authenticates as the dedicated ``baileys`` ACL user.

    Args:
        relais_home: RELAIS_HOME directory.
        redis_pass_baileys: Password for the baileys Redis ACL user.
        role: API key role (user or admin).
        label: Label for the key.

    Returns:
        ApiKeyResult with the generated hex key.
    """
    if not redis_pass_baileys:
        return ApiKeyResult(
            ok=False,
            detail="REDIS_PASS_BAILEYS is not set. Add it to .env before generating an API key.",
        )

    vendor_dir = relais_home / "vendor" / "baileys-api"
    if not vendor_dir.is_dir():
        return ApiKeyResult(
            ok=False,
            detail=f"baileys-api vendor not found at {vendor_dir}. Run install first.",
        )

    redis_url = f"redis://baileys:{redis_pass_baileys}@localhost:6379"

    env = {**os.environ, "REDIS_URL": redis_url}

    try:
        result = subprocess.run(
            ["bun", "scripts/manage-api-keys.ts", "create", role, label],
            cwd=vendor_dir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        return ApiKeyResult(ok=False, detail=f"manage-api-keys failed: {exc.stderr or str(exc)}")
    except subprocess.TimeoutExpired:
        return ApiKeyResult(ok=False, detail="manage-api-keys timed out")

    if result.returncode != 0:
        return ApiKeyResult(ok=False, detail=f"manage-api-keys failed (exit {result.returncode}): {result.stderr or result.stdout}")

    # Parse "Created API key with role 'user': <hex>"
    match = re.search(r":\s*([a-f0-9]+)\s*$", result.stdout, re.MULTILINE)
    if not match:
        return ApiKeyResult(ok=False, detail=f"Could not parse API key from output: {result.stdout[:200]}")

    return ApiKeyResult(ok=True, api_key=match.group(1), detail="API key generated")


# ---------------------------------------------------------------------------
# HTTP helpers (async)
# ---------------------------------------------------------------------------

async def _http_get(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    """GET with timeout. Returns (ok, detail)."""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    return True, "ok"
                return False, f"HTTP {resp.status}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def _http_post(
    url: str,
    json_data: dict | None = None,
    headers: dict | None = None,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    """POST with timeout. Returns (ok, detail)."""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=json_data, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status < 400:
                    return True, "accepted"
                body = await resp.text()
                return False, f"HTTP {resp.status}: {body[:200]}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def _http_delete(
    url: str,
    headers: dict | None = None,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    """DELETE with timeout. Treats 404 as success (idempotent)."""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 200:
                    return True, "logged out"
                if resp.status == 404:
                    return True, "already disconnected"
                body = await resp.text()
                return False, f"HTTP {resp.status}: {body[:200]}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Pair / Unpair
# ---------------------------------------------------------------------------

async def pair(
    params: PairParams,
    phone_number: str,
    api_key: str,
    gateway_url: str,
    webhook_host: str,
    webhook_port: str,
    webhook_secret: str,
    redis_client: Any,
) -> StepResult:
    """Execute the full pairing flow: health → gateway POST → Redis SET.

    Args:
        params: Routing context for the QR relay.
        phone_number: Bot phone in E.164.
        api_key: baileys-api key.
        gateway_url: baileys-api base URL.
        webhook_host: Adapter webhook host.
        webhook_port: Adapter webhook port.
        webhook_secret: Shared webhook secret.
        redis_client: Async Redis connection (must support .set()).

    Returns:
        StepResult with pairing status.
    """
    # Health check
    ok, detail = await _http_get(f"http://{webhook_host}:{webhook_port}/health")
    if not ok:
        return StepResult(ok=False, detail=f"Adapter unreachable: {detail}")

    # Gateway POST
    url = f"{gateway_url}/connections/{phone_number}"
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "webhookUrl": f"http://{webhook_host}:{webhook_port}/webhook",
        "webhookVerifyToken": webhook_secret,
        "includeMedia": False,
        "syncFullHistory": False,
    }
    ok, detail = await _http_post(url, json_data=payload, headers=headers)
    if not ok:
        return StepResult(ok=False, detail=f"Gateway rejected: {detail}")

    # Redis SET
    try:
        from common.streams import KEY_WHATSAPP_PAIRING
    except ImportError:
        KEY_WHATSAPP_PAIRING = "relais:whatsapp:pairing"

    context = json.dumps({
        "sender_id": params.sender_id,
        "channel": params.channel,
        "session_id": params.session_id,
        "correlation_id": params.correlation_id,
        "reply_to": params.reply_to,
        "state": "pending_qr",
        "timestamp": time.time(),
    })

    try:
        await redis_client.set(KEY_WHATSAPP_PAIRING, context, ex=300)
    except Exception as exc:
        return StepResult(ok=False, detail=f"Redis write failed: {exc}")

    return StepResult(ok=True, detail="Pairing initiated — QR will appear asynchronously")


async def unpair(
    phone_number: str,
    api_key: str,
    gateway_url: str,
    redis_client: Any,
) -> StepResult:
    """Execute the unpair flow: gateway DELETE → Redis DEL.

    Args:
        phone_number: Bot phone in E.164.
        api_key: baileys-api key.
        gateway_url: baileys-api base URL.
        redis_client: Async Redis connection.

    Returns:
        StepResult.
    """
    # Gateway DELETE
    url = f"{gateway_url}/connections/{phone_number}"
    headers = {"x-api-key": api_key}
    ok, detail = await _http_delete(url, headers=headers)
    if not ok:
        return StepResult(ok=False, detail=f"Gateway rejected: {detail}")

    # Redis cleanup
    try:
        from common.streams import KEY_WHATSAPP_PAIRING
    except ImportError:
        KEY_WHATSAPP_PAIRING = "relais:whatsapp:pairing"

    try:
        await redis_client.delete(KEY_WHATSAPP_PAIRING)
    except Exception as exc:
        return StepResult(ok=False, detail=f"Redis cleanup failed: {exc}")

    return StepResult(ok=True, detail=f"WhatsApp unlinked ({detail})")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

async def check_health(
    gateway_url: str,
    webhook_host: str,
    webhook_port: str,
) -> StepResult:
    """Probe both the adapter webhook and the baileys-api gateway.

    Returns:
        StepResult with combined health status.
    """
    issues: list[str] = []

    ok, detail = await _http_get(f"http://{webhook_host}:{webhook_port}/health")
    if not ok:
        issues.append(f"adapter: {detail}")

    ok, detail = await _http_get(f"{gateway_url}/status")
    if not ok:
        issues.append(f"gateway: {detail}")

    if issues:
        return StepResult(ok=False, detail="; ".join(issues))
    return StepResult(ok=True, detail="adapter and gateway healthy")


# ---------------------------------------------------------------------------
# Supervisor control — encapsulates iteration C fix
# ---------------------------------------------------------------------------

def supervisor_ctl(
    action: str,
    service: str | None = None,
    project_root: Path | None = None,
) -> StepResult:
    """Run a supervisorctl command with correct cwd and config flag.

    Args:
        action: supervisorctl subcommand (start, stop, restart, status, pid, tail).
        service: Service name (e.g. "optional:baileys-api"). None for
            commands that don't need one (like "pid").
        project_root: Project root directory.  If None, resolved from
            RELAIS_HOME.

    Returns:
        StepResult with stdout as detail.
    """
    if project_root is None:
        project_root = resolve_project_root()

    cmd = ["supervisorctl", "-c", "supervisord.conf", action]
    if service:
        cmd.append(service)

    try:
        result = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return StepResult(
                ok=False,
                detail=result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}",
            )
        return StepResult(ok=True, detail=result.stdout.strip())
    except subprocess.CalledProcessError as exc:
        return StepResult(ok=False, detail=str(exc.stderr or exc))
    except subprocess.TimeoutExpired:
        return StepResult(ok=False, detail="supervisorctl timed out")
    except FileNotFoundError:
        return StepResult(ok=False, detail="supervisorctl not found on PATH")


# ---------------------------------------------------------------------------
# .env manipulation
# ---------------------------------------------------------------------------

def write_env_var(key: str, value: str, env_file: Path) -> StepResult:
    """Set or update a variable in a .env file.

    Creates the file if it doesn't exist.  Preserves comments and
    ordering.

    Args:
        key: Variable name (e.g. WHATSAPP_PHONE_NUMBER).
        value: Variable value.
        env_file: Path to the .env file.

    Returns:
        StepResult.
    """
    if not env_file.exists():
        env_file.write_text(f"{key}={value}\n")
        return StepResult(ok=True, detail=f"{key} set (file created)")

    lines = env_file.read_text().splitlines(keepends=True)
    pattern = re.compile(rf"^{re.escape(key)}\s*=")
    found = False
    new_lines: list[str] = []
    for line in lines:
        if pattern.match(line):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        new_lines.append(f"{key}={value}\n")

    env_file.write_text("".join(new_lines))
    return StepResult(ok=True, detail=f"{key} {'updated' if found else 'set'}")


# ---------------------------------------------------------------------------
# Channel config toggle
# ---------------------------------------------------------------------------

def enable_channel(relais_home: Path) -> StepResult:
    """Set whatsapp.enabled=true in aiguilleur.yaml."""
    return _toggle_channel(relais_home, enabled=True)


def disable_channel(relais_home: Path) -> StepResult:
    """Set whatsapp.enabled=false in aiguilleur.yaml."""
    return _toggle_channel(relais_home, enabled=False)


def _toggle_channel(relais_home: Path, enabled: bool) -> StepResult:
    """Toggle the whatsapp channel in aiguilleur.yaml.

    Args:
        relais_home: RELAIS_HOME directory.
        enabled: Target state.

    Returns:
        StepResult.
    """
    import yaml

    config_path = relais_home / "config" / "aiguilleur.yaml"
    if not config_path.is_file():
        return StepResult(ok=False, detail=f"Config not found: {config_path}")

    data = yaml.safe_load(config_path.read_text())
    if not isinstance(data, dict):
        return StepResult(ok=False, detail="Invalid aiguilleur.yaml format")

    channels = data.get("channels", {})
    if "whatsapp" not in channels:
        return StepResult(ok=False, detail="No whatsapp entry in aiguilleur.yaml")

    channels["whatsapp"]["enabled"] = enabled

    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    state = "enabled" if enabled else "disabled"
    return StepResult(ok=True, detail=f"WhatsApp channel {state}")
