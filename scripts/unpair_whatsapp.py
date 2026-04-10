#!/usr/bin/env python3
"""Unlink (logout) the WhatsApp session from the Baileys gateway.

This is the symmetric counterpart of ``pair_whatsapp.py``.  It is
invoked by the ``relais-config`` subagent (via its ``execute`` shell
tool) as part of the ``channel-setup`` skill when the user asks to
unlink, logout, or disconnect WhatsApp.

Responsibilities:
  1. Validate required environment variables (phone number, API key).
  2. Call ``DELETE /connections/:phoneNumber`` on baileys-api to log
     out the WhatsApp session and evict the Baileys credentials from
     the gateway's Redis session storage.
  3. Delete any stale pairing routing context (``relais:whatsapp:pairing``)
     from Redis so a subsequent pairing starts from a clean state.

After this script runs, the linked device disappears from the user's
WhatsApp > Settings > Linked Devices.  To reconnect, run
``scripts/pair_whatsapp.py`` again.

This script does NOT:
  - Disable the channel in ``aiguilleur.yaml`` (the subagent does that
    when the user confirms).
  - Stop the baileys-api supervisord process (the subagent does that
    when the user confirms).
  - Touch any Baileys-internal Redis keys (the gateway handles its own
    cleanup on logout — the session data lives under the
    ``baileys-api:*`` ACL namespace which RELAIS cannot write to).

Exit codes:
  0 — logout successful (or connection was already absent)
  1 — missing / invalid arguments or env vars
  3 — gateway DELETE failed (baileys-api down, auth rejected, etc.)
  4 — Redis cleanup failed

Usage:
  python scripts/unpair_whatsapp.py              # uses WHATSAPP_PHONE_NUMBER
  python scripts/unpair_whatsapp.py --phone-number +33612345678

Environment variables read:
  WHATSAPP_PHONE_NUMBER       (required unless --phone-number is passed)
  WHATSAPP_API_KEY            (required, from baileys-api manage-api-keys.ts)
  WHATSAPP_GATEWAY_URL        (default: http://localhost:3025)
  REDIS_SOCKET_PATH           (default: $RELAIS_HOME/redis.sock)
  REDIS_PASS_COMMANDANT       (required, Redis ACL user `commandant`)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass

logger = logging.getLogger("relais.unpair_whatsapp")


# ---------------------------------------------------------------------------
# Exit codes (mirrors pair_whatsapp.py for consistency)
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_BAD_ARGS = 1
EXIT_GATEWAY_FAILED = 3
EXIT_REDIS_FAILED = 4


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UnpairEnv:
    """Environment variables required for the unpair flow.

    Attributes:
        phone_number: The bot's WhatsApp phone number in E.164 format.
        api_key: API key for the Baileys gateway (``x-api-key`` header).
        gateway_url: Base URL of the baileys-api service.
    """

    phone_number: str
    api_key: str
    gateway_url: str

    @classmethod
    def from_environ(cls, override_phone: str | None = None) -> "UnpairEnv":
        """Read env vars, letting an explicit CLI phone number override the env.

        Args:
            override_phone: Optional phone number from ``--phone-number``.
                When non-empty, takes precedence over ``WHATSAPP_PHONE_NUMBER``.

        Returns:
            A populated ``UnpairEnv``.  Missing required vars become empty
            strings and must be validated by the caller.
        """
        phone = (override_phone or "").strip() or os.environ.get("WHATSAPP_PHONE_NUMBER", "").strip()
        return cls(
            phone_number=phone,
            api_key=os.environ.get("WHATSAPP_API_KEY", "").strip(),
            gateway_url=os.environ.get("WHATSAPP_GATEWAY_URL", "http://localhost:3025").rstrip("/"),
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
        prog="unpair_whatsapp.py",
        description="Unlink the WhatsApp session from baileys-api (symmetric to pair_whatsapp.py).",
    )
    parser.add_argument(
        "--phone-number",
        default="",
        help="WhatsApp phone number (E.164). Overrides WHATSAPP_PHONE_NUMBER env var.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

async def call_gateway_delete_connection(env: UnpairEnv) -> tuple[bool, str]:
    """Issue ``DELETE /connections/:phoneNumber`` against baileys-api.

    The endpoint returns HTTP 200 on success and HTTP 404 when the
    connection does not exist — both cases are treated as success by
    this function (unpair is idempotent).

    Args:
        env: Environment configuration.

    Returns:
        A ``(ok, detail)`` tuple.  ``ok`` is True when the gateway
        accepts the delete (HTTP 200) or reports the connection is
        already gone (HTTP 404).
    """
    import aiohttp

    url = f"{env.gateway_url}/connections/{env.phone_number}"
    headers = {"x-api-key": env.api_key}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    return True, "logged out"
                if resp.status == 404:
                    return True, "already disconnected"
                body = await resp.text()
                return False, f"HTTP {resp.status}: {body[:200]}"
    except asyncio.TimeoutError:
        return False, "timeout contacting gateway"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Redis helper
# ---------------------------------------------------------------------------

async def delete_pairing_context() -> tuple[bool, str]:
    """Delete the WhatsApp pairing routing context from Redis.

    Idempotent — succeeds even if the key is absent.  Connects as the
    ``commandant`` ACL user (the only user with write access to
    ``relais:whatsapp:*`` in ``config/redis.conf``).

    Returns:
        A ``(ok, detail)`` tuple.
    """
    from common.redis_client import RedisClient
    from common.streams import KEY_WHATSAPP_PAIRING

    client = RedisClient("commandant")
    try:
        conn = await client.get_connection()
        removed = await conn.delete(KEY_WHATSAPP_PAIRING)
        return True, f"deleted ({removed} key(s))"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    """Execute the full unpair flow.

    Args:
        args: Parsed CLI arguments.

    Returns:
        A process exit code (one of the ``EXIT_*`` constants).
    """
    env = UnpairEnv.from_environ(override_phone=args.phone_number)

    # --- Validation ---
    if not env.phone_number:
        print(
            "ERROR: phone number not provided.\n"
            "Pass --phone-number +33XXXXXXXXX or set WHATSAPP_PHONE_NUMBER in .env.",
            file=sys.stderr,
        )
        return EXIT_BAD_ARGS
    if not env.api_key:
        print("ERROR: WHATSAPP_API_KEY is not set.", file=sys.stderr)
        return EXIT_BAD_ARGS

    # --- Gateway DELETE ---
    print(f"Unlinking {env.phone_number} from baileys-api ({env.gateway_url})...")
    ok, detail = await call_gateway_delete_connection(env)
    if not ok:
        print(f"ERROR: Gateway rejected the delete ({detail}).", file=sys.stderr)
        print(
            "Is baileys-api running? Try: supervisorctl start optional:baileys-api",
            file=sys.stderr,
        )
        return EXIT_GATEWAY_FAILED
    print(f"Gateway: {detail}.")

    # --- Redis cleanup (idempotent) ---
    print("Cleaning up pairing routing context in Redis...")
    ok, detail = await delete_pairing_context()
    if not ok:
        print(f"ERROR: Failed to clean Redis ({detail}).", file=sys.stderr)
        return EXIT_REDIS_FAILED
    print(f"Redis: {detail}.")

    print(
        "WhatsApp unlinked. The device no longer appears in "
        "WhatsApp > Settings > Linked Devices."
    )
    print("To reconnect, run: python scripts/pair_whatsapp.py …")
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
