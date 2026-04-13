"""CLI entry point for the WhatsApp channel pack.

Usage::

    python -m channels.whatsapp install --phone +33612345678
    python -m channels.whatsapp configure --action health
    python -m channels.whatsapp configure --action pair --sender-id discord:123 ...
    python -m channels.whatsapp uninstall
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from channels.whatsapp import core


def _print_result(data: dict) -> None:
    """Print result as formatted JSON."""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_install(args: argparse.Namespace) -> int:
    """Run the install pipeline."""
    relais_home = Path(str(core.resolve_relais_home()))
    result = core.MultiStepResult()

    ok, detail = core.ensure_bun()
    result.add("ensure_bun", "done" if ok else "failed", detail)
    if not ok:
        _print_result({"ok": False, "steps": result.steps})
        return 1

    ok, detail = core.ensure_git()
    result.add("ensure_git", "done" if ok else "failed", detail)
    if not ok:
        _print_result({"ok": False, "steps": result.steps})
        return 1

    r = core.install_baileys(relais_home=relais_home)
    status = "skipped" if r.already_present else ("done" if r.ok else "failed")
    result.add("install_baileys", status, r.detail)
    if not r.ok:
        _print_result({"ok": False, "steps": result.steps})
        return 1

    redis_pass = os.environ.get("REDIS_PASS_BAILEYS", "")
    kr = core.generate_api_key(relais_home=relais_home, redis_pass_baileys=redis_pass)
    result.add("generate_api_key", "done" if kr.ok else "failed", kr.detail)
    if not kr.ok:
        _print_result({"ok": False, "steps": result.steps})
        return 1

    project_root = core.resolve_project_root(relais_home)
    env_file = project_root / ".env"

    for key, value in [
        ("WHATSAPP_PHONE_NUMBER", args.phone),
        ("WHATSAPP_API_KEY", kr.api_key),
        ("WHATSAPP_WEBHOOK_SECRET", args.webhook_secret),
    ]:
        core.write_env_var(key, value, env_file)
        result.add(f"write_{key}", "done", "set")

    core.enable_channel(relais_home)
    result.add("enable_channel", "done", "enabled")

    _print_result({"ok": result.ok, "steps": result.steps})
    return 0 if result.ok else 1


def cmd_configure(args: argparse.Namespace) -> int:
    """Run a configure action."""
    action = args.action
    relais_home = Path(str(core.resolve_relais_home()))
    env = {
        "phone_number": os.environ.get("WHATSAPP_PHONE_NUMBER", ""),
        "api_key": os.environ.get("WHATSAPP_API_KEY", ""),
        "gateway_url": os.environ.get("WHATSAPP_GATEWAY_URL", "http://localhost:3025"),
        "webhook_host": os.environ.get("WHATSAPP_WEBHOOK_HOST", "127.0.0.1"),
        "webhook_port": os.environ.get("WHATSAPP_WEBHOOK_PORT", "8765"),
        "webhook_secret": os.environ.get("WHATSAPP_WEBHOOK_SECRET", ""),
    }

    if action == "health":
        r = asyncio.run(core.check_health(
            gateway_url=env["gateway_url"],
            webhook_host=env["webhook_host"],
            webhook_port=env["webhook_port"],
        ))
        _print_result({"ok": r.ok, "action": "health", "detail": r.detail})
        return 0 if r.ok else 1

    if action == "status":
        project_root = core.resolve_project_root(relais_home)
        r1 = core.supervisor_ctl("status", "optional:baileys-api", project_root=project_root)
        r2 = core.supervisor_ctl("status", "aiguilleur", project_root=project_root)
        _print_result({
            "ok": True, "action": "status",
            "detail": f"baileys-api: {r1.detail}; aiguilleur: {r2.detail}",
        })
        return 0

    if action == "pair":
        from common.redis_client import RedisClient

        params = core.PairParams(
            sender_id=args.sender_id,
            channel=args.channel,
            session_id=args.session_id,
            correlation_id=args.correlation_id,
            reply_to=args.reply_to,
        )

        async def _pair() -> core.StepResult:
            client = RedisClient("commandant")
            conn = await client.get_connection()
            try:
                return await core.pair(
                    params=params,
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
        _print_result({"ok": r.ok, "action": "pair", "detail": r.detail})
        return 0 if r.ok else 1

    if action == "unpair":
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
        _print_result({"ok": r.ok, "action": "unpair", "detail": r.detail})
        return 0 if r.ok else 1

    if action in ("enable", "disable"):
        fn = core.enable_channel if action == "enable" else core.disable_channel
        r = fn(relais_home)
        if r.ok:
            project_root = core.resolve_project_root(relais_home)
            core.supervisor_ctl("restart", "aiguilleur", project_root=project_root)
        _print_result({"ok": r.ok, "action": action, "detail": r.detail})
        return 0 if r.ok else 1

    print(f"Unknown action: {action}", file=sys.stderr)
    return 1


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Run the uninstall pipeline."""
    relais_home = Path(str(core.resolve_relais_home()))
    project_root = core.resolve_project_root(relais_home)
    result = core.MultiStepResult()

    # Stop services
    r = core.supervisor_ctl("stop", "optional:baileys-api", project_root=project_root)
    result.add("stop_baileys", "done" if r.ok else "failed", r.detail)

    r = core.disable_channel(relais_home)
    result.add("disable_channel", "done" if r.ok else "failed", r.detail)

    r = core.supervisor_ctl("restart", "aiguilleur", project_root=project_root)
    result.add("restart_aiguilleur", "done" if r.ok else "failed", r.detail)

    _print_result({"ok": result.ok, "steps": result.steps})
    return 0 if result.ok else 1


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="python -m channels.whatsapp",
        description="WhatsApp channel management for RELAIS",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # install
    p_install = sub.add_parser("install", help="Install WhatsApp channel")
    p_install.add_argument("--phone", required=True, help="Phone number (E.164)")
    p_install.add_argument("--webhook-secret", required=True, help="Webhook secret (min 16 chars)")

    # configure
    p_configure = sub.add_parser("configure", help="Configure WhatsApp channel")
    p_configure.add_argument("--action", required=True,
                             choices=["pair", "unpair", "health", "status", "enable", "disable"])
    p_configure.add_argument("--sender-id", default="")
    p_configure.add_argument("--channel", default="")
    p_configure.add_argument("--session-id", default="")
    p_configure.add_argument("--correlation-id", default="")
    p_configure.add_argument("--reply-to", default="")

    # uninstall
    sub.add_parser("uninstall", help="Uninstall WhatsApp channel")

    args = parser.parse_args()

    if args.command == "install":
        sys.exit(cmd_install(args))
    elif args.command == "configure":
        sys.exit(cmd_configure(args))
    elif args.command == "uninstall":
        sys.exit(cmd_uninstall(args))


if __name__ == "__main__":
    main()
