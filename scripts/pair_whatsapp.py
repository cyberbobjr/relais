#!/usr/bin/env python3
"""Initiate the WhatsApp QR pairing flow.

This script extracts the deterministic pairing logic that used to live in
``commandant/commands.py`` (`_handle_settings_whatsapp`).  It is designed to
be invoked by the ``relais-config`` subagent (via its ``execute`` shell tool)
as part of the ``channel-setup`` skill.

Responsibilities:
  1. Validate required environment variables (phone number, API key).
  2. Perform a health check against the WhatsApp adapter webhook.
  3. POST a connection creation request to the Baileys gateway.
  4. Store the pairing routing context in Redis (``relais:whatsapp:pairing``)
     so that the adapter can route the async QR code back to the originating
     channel.

The QR code itself is delivered asynchronously by the adapter via webhook
push events, NOT by this script.  The script only primes the pairing
context and returns success/failure.

Exit codes:
  0 — pairing initiated, QR will appear asynchronously
  1 — missing / invalid arguments or env vars
  2 — adapter webhook unreachable or unhealthy
  3 — gateway POST failed (baileys-api down, auth rejected, etc.)
  4 — Redis write failed

Usage:
  python scripts/pair_whatsapp.py \\
      --sender-id discord:12345 \\
      --channel discord \\
      --session-id sess-abc \\
      --correlation-id corr-xyz \\
      --reply-to 12345

Environment variables read:
  WHATSAPP_PHONE_NUMBER       (required, E.164 format e.g. +33612345678)
  WHATSAPP_API_KEY            (required, from baileys-api manage-api-keys.ts)
  WHATSAPP_GATEWAY_URL        (default: http://localhost:3025)
  WHATSAPP_WEBHOOK_HOST       (default: 127.0.0.1)
  WHATSAPP_WEBHOOK_PORT       (default: 8765)
  WHATSAPP_WEBHOOK_SECRET     (required, shared with baileys-api)
  REDIS_SOCKET_PATH           (default: $RELAIS_HOME/redis.sock)
  REDIS_PASS_COMMANDANT       (required, Redis ACL user `commandant`)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass

logger = logging.getLogger("relais.pair_whatsapp")


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_BAD_ARGS = 1
EXIT_ADAPTER_UNREACHABLE = 2
EXIT_GATEWAY_FAILED = 3
EXIT_REDIS_FAILED = 4


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PairingRoute:
    """Routing context required to relay the async QR back to the user.

    Attributes:
        sender_id: Full sender identifier (e.g. ``"discord:12345"``).
        channel: Channel name used to select the outgoing Redis stream.
        session_id: Session identifier for envelope threading.
        correlation_id: Correlation identifier for request tracking.
        reply_to: Channel-specific reply destination (e.g. Discord channel ID).
    """

    sender_id: str
    channel: str
    session_id: str
    correlation_id: str
    reply_to: str

    def to_redis_json(self) -> str:
        """Serialise to the JSON blob expected by the WhatsApp adapter.

        Returns:
            A JSON string matching the ``_parse_pairing`` contract in
            ``aiguilleur/channels/whatsapp/adapter.py``.
        """
        return json.dumps(
            {
                "sender_id": self.sender_id,
                "channel": self.channel,
                "session_id": self.session_id,
                "correlation_id": self.correlation_id,
                "reply_to": self.reply_to,
                "state": "pending_qr",
                "timestamp": time.time(),
            }
        )


@dataclass(frozen=True)
class WhatsAppEnv:
    """Environment variables read from the process.

    Attributes:
        phone_number: The bot's WhatsApp phone number in E.164 format.
        api_key: API key for the Baileys gateway (``x-api-key`` header).
        gateway_url: Base URL of the baileys-api service.
        webhook_host: Host on which the adapter listens for webhooks.
        webhook_port: Port on which the adapter listens for webhooks.
        webhook_secret: Shared secret used by the gateway to authenticate webhooks.
    """

    phone_number: str
    api_key: str
    gateway_url: str
    webhook_host: str
    webhook_port: str
    webhook_secret: str

    @classmethod
    def from_environ(cls) -> "WhatsAppEnv":
        """Read all WhatsApp env vars from the current process environment.

        Returns:
            A populated ``WhatsAppEnv``.  Missing optional vars fall back
            to documented defaults; required vars may be empty strings and
            must be validated by the caller.
        """
        return cls(
            phone_number=os.environ.get("WHATSAPP_PHONE_NUMBER", "").strip(),
            api_key=os.environ.get("WHATSAPP_API_KEY", "").strip(),
            gateway_url=os.environ.get("WHATSAPP_GATEWAY_URL", "http://localhost:3025").rstrip("/"),
            webhook_host=os.environ.get("WHATSAPP_WEBHOOK_HOST", "127.0.0.1"),
            webhook_port=os.environ.get("WHATSAPP_WEBHOOK_PORT", "8765"),
            webhook_secret=os.environ.get("WHATSAPP_WEBHOOK_SECRET", ""),
        )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments.

    Args:
        argv: Optional argument vector for testing.  Defaults to sys.argv[1:].

    Returns:
        The populated argparse Namespace.
    """
    parser = argparse.ArgumentParser(
        prog="pair_whatsapp.py",
        description="Initiate the WhatsApp QR pairing flow (extracted from /settings whatsapp).",
    )
    parser.add_argument("--sender-id", required=True, help="Full sender id, e.g. discord:12345")
    parser.add_argument("--channel", required=True, help="Channel name (e.g. discord, telegram)")
    parser.add_argument("--session-id", required=True, help="Session identifier")
    parser.add_argument("--correlation-id", required=True, help="Correlation identifier")
    parser.add_argument("--reply-to", required=True, help="Channel-specific reply destination")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def check_adapter_health(env: WhatsAppEnv) -> tuple[bool, str]:
    """Call the WhatsApp adapter ``/health`` endpoint.

    Args:
        env: Environment configuration.

    Returns:
        A ``(ok, detail)`` tuple.  ``ok`` is True when the adapter responds
        with HTTP 200; ``detail`` is a human-readable diagnostic string.
    """
    import aiohttp

    url = f"http://{env.webhook_host}:{env.webhook_port}/health"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    return True, "ok"
                return False, f"HTTP {resp.status}"
    except asyncio.TimeoutError:
        return False, "timeout"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def call_gateway_create_connection(env: WhatsAppEnv) -> tuple[bool, str]:
    """POST a connection creation request to baileys-api.

    Args:
        env: Environment configuration.

    Returns:
        A ``(ok, detail)`` tuple.  ``ok`` is True when the gateway accepts
        the request (HTTP < 400); ``detail`` carries the error message when
        not ok.
    """
    import aiohttp

    url = f"{env.gateway_url}/connections/{env.phone_number}"
    headers = {"x-api-key": env.api_key, "Content-Type": "application/json"}
    payload = {
        "webhookUrl": f"http://{env.webhook_host}:{env.webhook_port}/webhook",
        "webhookVerifyToken": env.webhook_secret,
        "includeMedia": False,
        "syncFullHistory": False,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    return False, f"HTTP {resp.status}: {body[:200]}"
                return True, "accepted"
    except asyncio.TimeoutError:
        return False, "timeout contacting gateway"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Redis helper
# ---------------------------------------------------------------------------

async def store_pairing_context(route: PairingRoute) -> tuple[bool, str]:
    """Write the pairing routing context to Redis with a 300s TTL.

    Connects as the ``commandant`` ACL user (the only user with write
    access to ``relais:whatsapp:*`` in ``config/redis.conf``).

    Args:
        route: Fully populated routing context.

    Returns:
        A ``(ok, detail)`` tuple.
    """
    from common.redis_client import RedisClient
    from common.streams import KEY_WHATSAPP_PAIRING

    client = RedisClient("commandant")
    try:
        conn = await client.get_connection()
        await conn.set(KEY_WHATSAPP_PAIRING, route.to_redis_json(), ex=300)
        return True, "stored"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    """Execute the full pairing flow.

    Args:
        args: Parsed CLI arguments.

    Returns:
        A process exit code (one of the ``EXIT_*`` constants).
    """
    env = WhatsAppEnv.from_environ()

    # --- Validation ---
    if not env.phone_number:
        print("ERROR: WHATSAPP_PHONE_NUMBER is not set.", file=sys.stderr)
        print("Set it in .env to the bot's number (e.g. +33612345678).", file=sys.stderr)
        return EXIT_BAD_ARGS
    if not env.api_key:
        print("ERROR: WHATSAPP_API_KEY is not set.", file=sys.stderr)
        print(
            "Generate one with: cd $RELAIS_HOME/vendor/baileys-api && "
            "bun scripts/manage-api-keys.ts create user relais-adapter",
            file=sys.stderr,
        )
        return EXIT_BAD_ARGS
    if not env.webhook_secret:
        print("ERROR: WHATSAPP_WEBHOOK_SECRET is not set.", file=sys.stderr)
        return EXIT_BAD_ARGS

    route = PairingRoute(
        sender_id=args.sender_id,
        channel=args.channel,
        session_id=args.session_id,
        correlation_id=args.correlation_id,
        reply_to=args.reply_to,
    )

    # --- Adapter health check ---
    print(f"Checking WhatsApp adapter health at {env.webhook_host}:{env.webhook_port}...")
    ok, detail = await check_adapter_health(env)
    if not ok:
        print(f"ERROR: WhatsApp adapter is not healthy ({detail}).", file=sys.stderr)
        print(
            "Enable whatsapp in aiguilleur.yaml and restart Aiguilleur, then retry.",
            file=sys.stderr,
        )
        return EXIT_ADAPTER_UNREACHABLE
    print("Adapter OK.")

    # --- Gateway POST ---
    print(f"Requesting connection from baileys-api ({env.gateway_url})...")
    ok, detail = await call_gateway_create_connection(env)
    if not ok:
        print(f"ERROR: Gateway rejected the request ({detail}).", file=sys.stderr)
        print(
            f"Is baileys-api running? Try: supervisorctl start optional:baileys-api",
            file=sys.stderr,
        )
        return EXIT_GATEWAY_FAILED
    print("Gateway accepted the connection request.")

    # --- Redis write (AFTER successful gateway call) ---
    print("Storing pairing routing context in Redis...")
    ok, detail = await store_pairing_context(route)
    if not ok:
        print(f"ERROR: Failed to store pairing context ({detail}).", file=sys.stderr)
        return EXIT_REDIS_FAILED

    print("Pairing initiated. The QR code will appear asynchronously in the chat.")
    print("Open WhatsApp > Settings > Linked Devices > Link a Device, then scan it.")
    return EXIT_OK


def main() -> None:
    """Entry point when invoked as a script."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
