"""Bundle management for the RELAIS TUI — Phase 5.

Provides local-filesystem operations for listing, installing, and removing
RELAIS bundles.  All operations act directly on ``RELAIS_HOME/bundles/``
without any REST calls.

Bundle ZIP structure::

    <bundle-name>/
        bundle.yaml          # required manifest
        ...                  # any additional files

bundle.yaml required fields::

    name: my-bundle
    description: |
      What this bundle does.
    version: "1.0.0"
    author: "Name"

Security guarantees
-------------------
* ZIP bomb protection: total uncompressed size must not exceed 50 MB.
* Path traversal protection: all ZIP members must resolve inside the
  target bundle directory after extraction.
"""

from __future__ import annotations

import logging
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

import yaml

_log = logging.getLogger(__name__)

_MAX_UNCOMPRESSED_BYTES: int = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BundleInfo:
    """Lightweight DTO representing a discovered or installed bundle.

    Args:
        name: Bundle identifier (from bundle.yaml ``name`` field).
        description: Human-readable description (from bundle.yaml).
        version: Semantic version string (from bundle.yaml).
        author: Bundle author (from bundle.yaml).
    """

    name: str
    description: str
    version: str
    author: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_bundles_dir() -> Path:
    """Return the RELAIS bundles directory, creating it if absent.

    Resolves ``$RELAIS_HOME/bundles/``, falling back to
    ``~/.relais/bundles/`` when ``RELAIS_HOME`` is not set.

    Returns:
        Absolute path to the bundles directory (guaranteed to exist after
        this call).
    """
    relais_home = os.environ.get("RELAIS_HOME", str(Path.home() / ".relais"))
    bundles_dir = Path(relais_home) / "bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)
    return bundles_dir


def list_bundles(bundles_dir: Path | None = None) -> list[BundleInfo]:
    """Scan *bundles_dir* for installed bundles and return sorted metadata.

    Each immediate sub-directory of *bundles_dir* that contains a valid
    ``bundle.yaml`` is returned as a :class:`BundleInfo`.  Directories
    whose manifest is missing or malformed are silently skipped.

    Args:
        bundles_dir: Directory to scan.  Defaults to :func:`get_bundles_dir`
            when ``None``.

    Returns:
        List of :class:`BundleInfo` instances sorted alphabetically by name.
    """
    if bundles_dir is None:
        bundles_dir = get_bundles_dir()

    results: list[BundleInfo] = []

    if not bundles_dir.exists():
        return results

    for entry in sorted(bundles_dir.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "bundle.yaml"
        if not manifest_path.exists():
            continue
        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            info = BundleInfo(
                name=str(raw.get("name", entry.name)),
                description=str(raw.get("description", "")),
                version=str(raw.get("version", "1.0.0")),
                author=str(raw.get("author", "")),
            )
            results.append(info)
        except Exception:
            _log.debug("Skipping bundle %s: invalid manifest", entry.name, exc_info=True)
            continue

    return results


def install_bundle(
    zip_path: Path | str,
    bundles_dir: Path | None = None,
) -> BundleInfo:
    """Install a bundle from a ZIP file into *bundles_dir*.

    The ZIP must contain exactly one root directory whose name matches the
    ``name`` field declared in ``bundle.yaml``.  Installation is atomic:
    files are first extracted to a staging directory, then renamed into
    place (replacing any existing installation of the same bundle).

    Args:
        zip_path: Path to the bundle ZIP file.
        bundles_dir: Destination directory.  Defaults to
            :func:`get_bundles_dir` when ``None``.

    Returns:
        :class:`BundleInfo` for the newly installed bundle.

    Raises:
        FileNotFoundError: If *zip_path* does not exist.
        ValueError: If the ZIP is invalid, exceeds the 50 MB size limit,
            contains a path-traversal member, or is missing ``bundle.yaml``.
    """
    zip_path = Path(zip_path)

    if not zip_path.exists():
        raise FileNotFoundError(f"ZIP file not found: {zip_path}")

    if bundles_dir is None:
        bundles_dir = get_bundles_dir()

    bundles_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        # --- ZIP bomb check -------------------------------------------------
        total_size = sum(info.file_size for info in zf.infolist())
        if total_size > _MAX_UNCOMPRESSED_BYTES:
            raise ValueError(
                f"ZIP bomb detected: total uncompressed size {total_size} bytes "
                f"exceeds limit of {_MAX_UNCOMPRESSED_BYTES} bytes."
            )

        # --- Locate bundle.yaml ---------------------------------------------
        manifest_member: str | None = None
        for member in zf.namelist():
            # Normalise separators and look for bundle.yaml at depth 1
            parts = Path(member).parts
            if len(parts) == 2 and parts[1] == "bundle.yaml":
                manifest_member = member
                break

        if manifest_member is None:
            raise ValueError(
                "bundle.yaml not found in ZIP.  "
                "The archive must contain <bundle-name>/bundle.yaml."
            )

        # --- Parse manifest -------------------------------------------------
        try:
            raw = yaml.safe_load(zf.read(manifest_member).decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"bundle.yaml is not valid YAML: {exc}") from exc

        if not isinstance(raw, dict) or not raw.get("name"):
            raise ValueError("bundle.yaml must define a 'name' field.")

        bundle_name: str = str(raw["name"])

        # --- Path traversal check -------------------------------------------
        dest_dir = (bundles_dir / bundle_name).resolve()
        for member in zf.infolist():
            member_path = (bundles_dir / member.filename).resolve()
            try:
                member_path.relative_to(dest_dir)
            except ValueError:
                raise ValueError(
                    f"Path traversal detected: member '{member.filename}' "
                    f"would be extracted outside the bundle directory."
                )

        # --- Atomic extraction ----------------------------------------------
        # Extract to a staging directory first, then rename atomically.
        staging = bundles_dir / f".staging-{bundle_name}"
        if staging.exists():
            shutil.rmtree(staging)

        staging_parent = bundles_dir / f".staging-parent-{bundle_name}"
        try:
            # Extract into a temporary parent so the bundle root lands at staging/
            staging_parent.mkdir(parents=True, exist_ok=True)
            zf.extractall(staging_parent)
            extracted = staging_parent / bundle_name
            extracted.rename(staging)

            # Atomically swap: remove existing, rename staging into place
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            staging.rename(dest_dir)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        finally:
            if staging_parent.exists():
                shutil.rmtree(staging_parent, ignore_errors=True)

    return BundleInfo(
        name=bundle_name,
        description=str(raw.get("description", "")),
        version=str(raw.get("version", "1.0.0")),
        author=str(raw.get("author", "")),
    )


def uninstall_bundle(name: str, bundles_dir: Path | None = None) -> None:
    """Remove an installed bundle directory.

    Args:
        name: The bundle name (must match the directory name under
            *bundles_dir*).
        bundles_dir: Bundles root directory.  Defaults to
            :func:`get_bundles_dir` when ``None``.

    Raises:
        FileNotFoundError: If no bundle named *name* is installed.
    """
    if bundles_dir is None:
        bundles_dir = get_bundles_dir()

    bundle_dir = bundles_dir / name
    if not bundle_dir.exists():
        raise FileNotFoundError(f"Bundle '{name}' is not installed in {bundles_dir}.")

    shutil.rmtree(bundle_dir)
    _log.info("Uninstalled bundle '%s'.", name)
