"""Bundle management command handlers for Commandant.

Implements /bundle install, /bundle uninstall, and /bundle list subcommands.
All replies are published to the channel's outgoing stream with
ACTION_MESSAGE_OUTGOING.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from common.bundle_errors import BundleError
from common.bundles import install_bundle, list_bundles, uninstall_bundle
from common.config_loader import resolve_bundles_dir
from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_OUTGOING
from common.streams import stream_outgoing

logger = logging.getLogger("commandant.bundle_commands")

_USAGE = (
    "Usage:\n"
    "  /bundle install /path/to/file.zip\n"
    "  /bundle uninstall <name>\n"
    "  /bundle list"
)


async def _send_reply(envelope: Envelope, redis_conn: Any, text: str) -> None:
    """Publish a text reply to the channel's outgoing stream.

    Args:
        envelope: The originating command envelope (used for channel / tracking).
        redis_conn: Active async Redis connection.
        text: Reply text to send.
    """
    response = Envelope.from_parent(envelope, text)
    response.action = ACTION_MESSAGE_OUTGOING
    await redis_conn.xadd(
        stream_outgoing(envelope.channel),
        {"payload": response.to_json()},
    )


async def handle_bundle_install(envelope: Envelope, redis_conn: Any, args: list[str]) -> None:
    """Install a bundle from a ZIP path.

    Parses the zip path from ``args``, calls ``install_bundle``, and sends a
    success or error reply to the channel.

    Args:
        envelope: The originating /bundle install envelope.
        redis_conn: Active async Redis connection.
        args: Remaining words after the 'install' subcommand token.
    """
    if not args:
        await _send_reply(
            envelope,
            redis_conn,
            f"Usage: /bundle install /path/to/file.zip\n\n{_USAGE}",
        )
        return

    zip_path = Path(args[0])
    bundles_dir = resolve_bundles_dir()
    try:
        manifest = install_bundle(zip_path, bundles_dir)
        await _send_reply(
            envelope,
            redis_conn,
            f"Bundle '{manifest.name}' v{manifest.version} installed successfully.",
        )
        logger.info("Bundle %r installed via /bundle install", manifest.name)
    except BundleError as exc:
        await _send_reply(envelope, redis_conn, f"Error: {exc}")
        logger.warning("Bundle install failed: %s", exc)


async def handle_bundle_uninstall(envelope: Envelope, redis_conn: Any, args: list[str]) -> None:
    """Uninstall a bundle by name.

    Parses the bundle name from ``args``, calls ``uninstall_bundle``, and sends
    a success or error reply to the channel.

    Args:
        envelope: The originating /bundle uninstall envelope.
        redis_conn: Active async Redis connection.
        args: Remaining words after the 'uninstall' subcommand token.
    """
    if not args:
        await _send_reply(
            envelope,
            redis_conn,
            f"Usage: /bundle uninstall <name>\n\n{_USAGE}",
        )
        return

    name = args[0]
    bundles_dir = resolve_bundles_dir()
    try:
        uninstall_bundle(name, bundles_dir)
        await _send_reply(
            envelope,
            redis_conn,
            f"Bundle '{name}' uninstalled successfully.",
        )
        logger.info("Bundle %r uninstalled via /bundle uninstall", name)
    except BundleError as exc:
        await _send_reply(envelope, redis_conn, f"Error: {exc}")
        logger.warning("Bundle uninstall failed: %s", exc)


async def handle_bundle_list(envelope: Envelope, redis_conn: Any) -> None:
    """List installed bundles.

    Calls ``list_bundles`` and sends a formatted reply to the channel. Sends
    a "no bundles installed" message when the list is empty.

    Args:
        envelope: The originating /bundle list envelope.
        redis_conn: Active async Redis connection.
    """
    bundles_dir = resolve_bundles_dir()
    manifests = list_bundles(bundles_dir)

    if not manifests:
        await _send_reply(envelope, redis_conn, "No bundles installed.")
        return

    lines = [f"Installed bundles ({len(manifests)}):"]
    for m in manifests:
        lines.append(f"  • {m.name} v{m.version} — {m.description}")
    await _send_reply(envelope, redis_conn, "\n".join(lines))


async def handle_bundle(envelope: Envelope, redis_conn: Any) -> None:
    """Dispatch a /bundle subcommand.

    Routes to ``handle_bundle_install``, ``handle_bundle_uninstall``, or
    ``handle_bundle_list`` based on the first word after /bundle. Sends usage
    help if the subcommand is missing or unknown.

    Usage::

        /bundle install /path/to/file.zip
        /bundle uninstall <name>
        /bundle list

    Args:
        envelope: The envelope whose ``content`` starts with "/bundle".
        redis_conn: Active async Redis connection.
    """
    # content is something like "/bundle install /tmp/foo.zip"
    # split into parts, first element is "/bundle"
    parts = envelope.content.strip().split()

    # parts[0] == "/bundle", parts[1] would be the subcommand
    if len(parts) < 2:
        await _send_reply(envelope, redis_conn, _USAGE)
        return

    subcommand = parts[1].lower()
    args = parts[2:]

    if subcommand == "install":
        await handle_bundle_install(envelope, redis_conn, args)
    elif subcommand == "uninstall":
        await handle_bundle_uninstall(envelope, redis_conn, args)
    elif subcommand == "list":
        await handle_bundle_list(envelope, redis_conn)
    else:
        await _send_reply(
            envelope,
            redis_conn,
            f"Unknown subcommand: '{subcommand}'\n\n{_USAGE}",
        )
