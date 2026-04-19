"""Bundle management CLI subcommands.

Implements the ``relais bundle`` command group using Python's built-in
``argparse`` (no additional dependencies required).

Sub-subcommands:
    install    Install a bundle from a ZIP file.
    uninstall  Remove an installed bundle by name.
    list       List all installed bundles.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from relais_tui.bundles import get_bundles_dir, install_bundle, list_bundles, uninstall_bundle


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_install(args: Any) -> int:
    """Install a bundle from a ZIP file.

    Validates the ZIP, extracts it, and prints a success message with the
    bundle name and version on stdout.

    Args:
        args: Parsed argument namespace; must have ``zip_file`` attribute
            containing the path to the bundle ZIP.

    Returns:
        0 on success, 1 on any error (validation failure, file not found,
        filesystem error).
    """
    bundles_dir: Path = get_bundles_dir()

    try:
        manifest = install_bundle(Path(args.zip_file), bundles_dir)
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    print(f"Installed bundle '{manifest.name}' version {manifest.version}.")
    return 0


def cmd_uninstall(args: Any) -> int:
    """Uninstall a bundle by name.

    Removes the bundle directory from the bundles installation directory.

    Args:
        args: Parsed argument namespace; must have ``name`` attribute
            containing the bundle name to remove.

    Returns:
        0 on success, 1 when the bundle is not installed.
    """
    bundles_dir: Path = get_bundles_dir()

    try:
        uninstall_bundle(args.name, bundles_dir)
    except FileNotFoundError as exc:
        print(f"Error: {exc}")
        return 1

    print(f"Uninstalled bundle '{args.name}'.")
    return 0


def cmd_list(args: Any) -> int:
    """List installed bundles.

    Prints one bundle per line in the format::

        <name>  <version>  <description_first_line>

    Prints ``No bundles installed.`` when no bundles are found.

    Args:
        args: Parsed argument namespace (no attributes consumed).

    Returns:
        0 always.
    """
    bundles_dir: Path = get_bundles_dir()
    manifests = list_bundles(bundles_dir)

    if not manifests:
        print("No bundles installed.")
        return 0

    # Compute column widths for aligned output
    name_width = max(len(m.name) for m in manifests)
    ver_width = max(len(m.version) for m in manifests)

    for manifest in manifests:
        first_line = manifest.description.splitlines()[0] if manifest.description else ""
        print(
            f"{manifest.name:<{name_width}}  "
            f"{manifest.version:<{ver_width}}  "
            f"{first_line}"
        )

    return 0


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


def add_bundle_subparser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add the ``bundle`` subparser with install/uninstall/list sub-subcommands.

    Registers ``bundle install``, ``bundle uninstall``, and ``bundle list``
    as sub-subcommands under the ``bundle`` subparser.  Each sub-subcommand
    sets ``args.func`` to its handler so callers can invoke it uniformly via
    ``sys.exit(args.func(args))``.

    Args:
        subparsers: The ``_SubParsersAction`` returned by
            ``parser.add_subparsers()``.
    """
    bundle_parser = subparsers.add_parser(
        "bundle",
        help="Manage RELAIS bundles.",
        description="Install, uninstall, or list RELAIS bundles.",
    )
    bundle_sub = bundle_parser.add_subparsers(
        dest="bundle_cmd",
        metavar="{install,uninstall,list}",
    )
    bundle_sub.required = True

    # -- install -------------------------------------------------------------
    install_parser = bundle_sub.add_parser(
        "install",
        help="Install a bundle from a ZIP file.",
        description="Validate and install a bundle ZIP to the bundles directory.",
    )
    install_parser.add_argument(
        "zip_file",
        metavar="ZIP_FILE",
        help="Path to the bundle ZIP file.",
    )
    install_parser.set_defaults(func=cmd_install)

    # -- uninstall -----------------------------------------------------------
    uninstall_parser = bundle_sub.add_parser(
        "uninstall",
        help="Uninstall a bundle by name.",
        description="Remove an installed bundle from the bundles directory.",
    )
    uninstall_parser.add_argument(
        "name",
        metavar="NAME",
        help="Name of the bundle to uninstall.",
    )
    uninstall_parser.set_defaults(func=cmd_uninstall)

    # -- list ----------------------------------------------------------------
    list_parser = bundle_sub.add_parser(
        "list",
        help="List installed bundles.",
        description="Print all installed bundles with their version and description.",
    )
    list_parser.set_defaults(func=cmd_list)
