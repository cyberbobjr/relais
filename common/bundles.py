"""Core bundle library for the RELAIS bundle system — Phase 1.

A *bundle* is a ZIP file containing a single root folder whose name matches
the bundle name declared in ``bundle.yaml``.  Bundles are installed to
``~/.relais/bundles/<bundle-name>/`` (or ``$RELAIS_HOME/bundles/``).

ZIP structure::

    <bundle-name>/
        bundle.yaml          # required manifest
        subagents/           # optional — SubagentRegistry picks these up
        skills/              # optional — available to all agents
        tools/               # optional — registers into global ToolRegistry

bundle.yaml format::

    name: my-bundle           # required, [a-z0-9][a-z0-9-]*
    description: |            # required, non-empty
      What this bundle does.
    version: "1.0.0"          # optional, default "1.0.0"
    author: "Name"            # optional, default ""
    tools: []                 # optional, tool names for conflict detection

Security guarantees
-------------------
* ZIP bomb protection: reject total uncompressed size > 50 MB.
* Path traversal protection: all ZIP members must resolve inside the bundle
  root after extraction.
"""

from __future__ import annotations

import logging
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

import yaml

from common.bundle_errors import (
    BundleInstallError,
    BundleNotFoundError,
    BundleValidationError,
)

logger = logging.getLogger(__name__)

# Maximum allowed total uncompressed size of a bundle ZIP (50 MB).
_MAX_UNCOMPRESSED_BYTES: int = 50 * 1024 * 1024

# Regex for valid bundle names: starts with [a-z0-9], followed by [a-z0-9-]*.
_NAME_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]*$")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BundleManifest:
    """Parsed and validated representation of a bundle's ``bundle.yaml``.

    All fields are immutable.  The ``name`` field is validated against
    ``[a-z0-9][a-z0-9-]*`` on construction — a ``BundleValidationError`` is
    raised for any name that does not match.

    Args:
        name: Bundle identifier.  Must match ``[a-z0-9][a-z0-9-]*``.
        description: Human-readable description.  Must be non-empty.
        version: Semantic version string.  Defaults to ``"1.0.0"``.
        author: Bundle author.  Defaults to ``""``.
        tools: Tool names exported by this bundle (used for conflict detection).

    Raises:
        BundleValidationError: If ``name`` fails the regex or ``description``
            is empty.
    """

    name: str
    description: str
    version: str = "1.0.0"
    author: str = ""
    tools: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate name and description after construction."""
        if not _NAME_RE.match(self.name):
            raise BundleValidationError(
                f"invalid bundle name {self.name!r}: must match [a-z0-9][a-z0-9-]*"
            )
        if not self.description.strip():
            raise BundleValidationError("bundle description must be non-empty")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install_bundle(zip_path: Path | str, bundles_dir: Path) -> BundleManifest:
    """Validate a bundle ZIP and extract it to ``bundles_dir``.

    Follows an 11-step pipeline:

    1. Verify ``zip_path`` exists and is a file.
    2. Verify it is a valid ZIP (``zipfile.is_zipfile``).
    3. Open ZIP and check total uncompressed size ≤ 50 MB.
    4. Determine single root directory (all members under one root).
    5. Check path traversal (no ``..`` in any member path after normalization).
    6. Find and parse ``<root>/bundle.yaml``.
    7. Validate manifest (name, description, name matches root dir).
    8. Check for tool name conflicts with existing bundles (log WARNING).
    9. Extract to a staging dir (``bundles_dir/.staging/<name>_<uuid>/``).
    10. Atomic rename: ``staging/<name>_<uuid>/<root>`` → ``bundles_dir/<name>``
        (replaces existing installation if present).
    11. Clean up staging dir; return manifest.

    Args:
        zip_path: Path to the bundle ZIP file.
        bundles_dir: Directory where bundles are installed.

    Returns:
        ``BundleManifest`` of the newly installed bundle.

    Raises:
        BundleValidationError: For any validation failure (invalid ZIP, ZIP
            bomb, path traversal, missing/invalid manifest, name mismatch).
        BundleInstallError: For filesystem errors during extraction or rename.
    """
    zip_path = Path(zip_path)

    # Step 1 — file existence
    if not zip_path.exists() or not zip_path.is_file():
        raise BundleValidationError(f"bundle ZIP not found: {zip_path}")

    # Step 2 — valid ZIP
    if not zipfile.is_zipfile(zip_path):
        raise BundleValidationError(
            f"{zip_path.name} is not a valid ZIP archive"
        )

    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = zf.infolist()

        # Step 3 — ZIP bomb guard
        total_uncompressed = sum(info.file_size for info in infos)
        if total_uncompressed > _MAX_UNCOMPRESSED_BYTES:
            raise BundleValidationError(
                f"bundle uncompressed size {total_uncompressed} bytes "
                f"exceeds the 50 MB limit"
            )

        # Step 4 — determine single root dir
        root_dir = _find_root_dir(infos)

        # Step 5 — path traversal guard
        _check_path_traversal(infos, root_dir)

        # Step 6 — find and parse bundle.yaml
        manifest_arc = f"{root_dir}/bundle.yaml"
        manifest_names = {info.filename for info in infos}
        if manifest_arc not in manifest_names:
            raise BundleValidationError(
                f"bundle.yaml not found inside ZIP (expected at {manifest_arc!r})"
            )
        raw_yaml = zf.read(manifest_arc).decode("utf-8")

        # Step 7 — validate manifest
        manifest = _parse_manifest(raw_yaml, expected_root=root_dir)

        # Step 8 — conflict detection
        _check_tool_conflicts(manifest, bundles_dir)

        # Step 9 — extract to staging
        staging_base = bundles_dir / ".staging"
        staging_base.mkdir(parents=True, exist_ok=True)
        staging_dir = staging_base / f"{manifest.name}_{uuid.uuid4().hex}"
        staging_dir.mkdir(parents=True, exist_ok=True)

        try:
            zf.extractall(staging_dir)
        except Exception as exc:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise BundleInstallError(
                f"failed to extract bundle {manifest.name!r}: {exc}"
            ) from exc

    # Step 10 — atomic rename
    extracted_bundle = staging_dir / root_dir
    dest = bundles_dir / manifest.name
    try:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(extracted_bundle), str(dest))
    except Exception as exc:
        raise BundleInstallError(
            f"failed to move bundle {manifest.name!r} to {dest}: {exc}"
        ) from exc
    finally:
        # Step 11 — clean up staging dir and remove staging_base if now empty
        shutil.rmtree(staging_dir, ignore_errors=True)
        try:
            staging_base.rmdir()  # Only succeeds if empty (no other concurrent installs)
        except OSError:
            pass  # Non-empty or concurrent install in progress — leave it

    logger.info("bundle %r installed to %s", manifest.name, dest)
    return manifest


def uninstall_bundle(name: str, bundles_dir: Path) -> None:
    """Remove an installed bundle directory.

    Args:
        name: Bundle name to uninstall.
        bundles_dir: Directory where bundles are installed.

    Raises:
        BundleNotFoundError: If ``bundles_dir/<name>`` does not exist.
    """
    bundle_path = bundles_dir / name
    if not bundle_path.exists():
        raise BundleNotFoundError(
            f"bundle {name!r} is not installed in {bundles_dir}"
        )
    shutil.rmtree(bundle_path)
    logger.info("bundle %r uninstalled from %s", name, bundles_dir)


def list_bundles(bundles_dir: Path) -> list[BundleManifest]:
    """Return all installed bundles sorted alphabetically by name.

    Silently skips subdirectories with a missing or invalid ``bundle.yaml``.

    Args:
        bundles_dir: Directory where bundles are installed.

    Returns:
        List of ``BundleManifest`` instances, sorted by ``name``.
    """
    if not bundles_dir.exists():
        return []

    results: list[BundleManifest] = []
    for entry in sorted(bundles_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        manifest = load_bundle_manifest(entry)
        if manifest is not None:
            results.append(manifest)

    return results


def load_bundle_manifest(bundle_dir: Path) -> Optional[BundleManifest]:
    """Read and parse ``bundle.yaml`` from ``bundle_dir``.

    Args:
        bundle_dir: Directory containing ``bundle.yaml``.

    Returns:
        Parsed ``BundleManifest``, or ``None`` if the file is absent or
        cannot be parsed.
    """
    yaml_path = bundle_dir / "bundle.yaml"
    if not yaml_path.is_file():
        return None
    try:
        raw = yaml_path.read_text(encoding="utf-8")
        return _parse_manifest(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_root_dir(infos: list[zipfile.ZipInfo]) -> str:
    """Determine the single root directory of a ZIP.

    Args:
        infos: List of ``ZipInfo`` members from the archive.

    Returns:
        Name of the root directory (no trailing slash).

    Raises:
        BundleValidationError: If no root directory is found or members span
            multiple root directories.
    """
    roots: set[str] = set()
    for info in infos:
        parts = Path(info.filename).parts
        if parts:
            roots.add(parts[0])

    if not roots:
        raise BundleValidationError("ZIP archive is empty")
    if len(roots) > 1:
        raise BundleValidationError(
            f"ZIP archive must have a single root directory, found: {sorted(roots)}"
        )
    return roots.pop()


def _check_path_traversal(infos: list[zipfile.ZipInfo], root_dir: str) -> None:
    """Raise BundleValidationError if any member escapes the root directory.

    Args:
        infos: List of ``ZipInfo`` members from the archive.
        root_dir: Expected root directory name.

    Raises:
        BundleValidationError: If any member path contains ``..`` or does not
            start with ``root_dir``.
    """
    for info in infos:
        raw = info.filename
        # ZIP paths are always POSIX — use PurePosixPath to avoid OS-dependent separators
        posix_path = PurePosixPath(raw)
        # Reject any literal ".." component
        if ".." in posix_path.parts:
            raise BundleValidationError(
                f"path traversal detected in ZIP member: {raw!r}"
            )
        # Verify the member stays inside root_dir using path parts (not string prefix)
        parts = posix_path.parts
        if not parts or parts[0] != root_dir:
            raise BundleValidationError(
                f"ZIP member {raw!r} is outside the bundle root {root_dir!r}"
            )


def _parse_manifest(raw_yaml: str, expected_root: str | None = None) -> BundleManifest:
    """Parse raw YAML string into a validated ``BundleManifest``.

    Args:
        raw_yaml: Contents of ``bundle.yaml``.
        expected_root: If provided, the manifest ``name`` must equal this value.

    Returns:
        Validated ``BundleManifest``.

    Raises:
        BundleValidationError: On YAML parse error, missing required fields,
            invalid name, or name/root mismatch.
    """
    try:
        data = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise BundleValidationError(f"bundle.yaml is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise BundleValidationError("bundle.yaml must be a YAML mapping")

    # Required fields
    name = data.get("name")
    description = data.get("description")
    if not name:
        raise BundleValidationError("bundle.yaml missing required field 'name'")
    if not description:
        raise BundleValidationError("bundle.yaml missing required field 'description'")

    # Optional fields with defaults
    version = str(data.get("version", "1.0.0"))
    author = str(data.get("author", ""))
    tools = list(data.get("tools") or [])

    # BundleManifest.__post_init__ validates the name regex
    manifest = BundleManifest(
        name=str(name),
        description=str(description).strip(),
        version=version,
        author=author,
        tools=tools,
    )

    # Step 7 — root dir / name mismatch
    if expected_root is not None and manifest.name != expected_root:
        raise BundleValidationError(
            f"bundle name mismatch: manifest declares {manifest.name!r} "
            f"but ZIP root directory is {expected_root!r}"
        )

    return manifest


def _check_tool_conflicts(manifest: BundleManifest, bundles_dir: Path) -> None:
    """Log WARNING for any tool name that already exists in another bundle.

    This never raises — conflicts are reported but installation proceeds.

    Args:
        manifest: Manifest of the bundle being installed.
        bundles_dir: Directory containing existing installed bundles.
    """
    if not manifest.tools:
        return

    incoming_tools = set(manifest.tools)
    for existing in list_bundles(bundles_dir):
        if existing.name == manifest.name:
            # Skip the bundle we're about to replace
            continue
        for tool_name in existing.tools:
            if tool_name in incoming_tools:
                logger.warning(
                    "tool name conflict: %r from bundle %r shadows existing "
                    "tool %r from bundle %r",
                    tool_name,
                    manifest.name,
                    tool_name,
                    existing.name,
                )
