"""Tests for the RELAIS bundle system — Phase 1: Core bundle library.

Tests are written FIRST (TDD RED phase) before any implementation.
All tests use tmp_path fixtures and in-memory ZIP construction.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers — build synthetic ZIP bytes in memory
# ---------------------------------------------------------------------------


def make_bundle_zip(
    bundle_name: str,
    manifest_overrides: dict | None = None,
    extra_members: list[tuple[str, bytes]] | None = None,
    root_dir_name: str | None = None,
    total_size_bytes: int = 0,
) -> bytes:
    """Build a valid (or intentionally broken) bundle ZIP in memory.

    Args:
        bundle_name: Name used in bundle.yaml ``name`` field.
        manifest_overrides: Dict merged into the default manifest YAML; use
            ``None`` values to delete a key.
        extra_members: Additional ``(arcname, content)`` tuples added verbatim.
        root_dir_name: Root directory name inside the ZIP.  Defaults to
            ``bundle_name``.
        total_size_bytes: If > 0, a padding file is added so the *uncompressed*
            total exceeds this value (useful to trigger ZIP bomb checks).

    Returns:
        Raw ZIP bytes.
    """
    root = root_dir_name or bundle_name
    manifest: dict = {
        "name": bundle_name,
        "description": f"Test bundle {bundle_name}",
        "version": "1.0.0",
        "author": "test",
        "tools": [],
    }
    if manifest_overrides:
        for k, v in manifest_overrides.items():
            if v is None:
                manifest.pop(k, None)
            else:
                manifest[k] = v

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(f"{root}/bundle.yaml", yaml.dump(manifest))
        if extra_members:
            for arcname, content in extra_members:
                zf.writestr(arcname, content)
        if total_size_bytes > 0:
            # Write a single large padding member to exceed uncompressed limit
            zf.writestr(f"{root}/padding.bin", b"\x00" * total_size_bytes)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bundles_dir(tmp_path: Path) -> Path:
    """Return a fresh temporary bundles directory."""
    d = tmp_path / "bundles"
    d.mkdir()
    return d


@pytest.fixture()
def zip_file(tmp_path: Path) -> Path:
    """Return a path to a valid bundle ZIP."""
    data = make_bundle_zip("my-bundle")
    p = tmp_path / "my-bundle.zip"
    p.write_bytes(data)
    return p


# ---------------------------------------------------------------------------
# Phase 1 tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_install_bundle_success(tmp_path: Path, bundles_dir: Path) -> None:
    """A valid ZIP installs correctly and returns the expected BundleManifest."""
    from common.bundles import BundleManifest, install_bundle

    zip_data = make_bundle_zip("my-bundle")
    zip_path = tmp_path / "my-bundle.zip"
    zip_path.write_bytes(zip_data)

    manifest = install_bundle(zip_path, bundles_dir)

    assert isinstance(manifest, BundleManifest)
    assert manifest.name == "my-bundle"
    assert manifest.description == "Test bundle my-bundle"
    assert manifest.version == "1.0.0"
    assert manifest.author == "test"
    assert manifest.tools == []
    # Directory must exist after install
    assert (bundles_dir / "my-bundle").is_dir()
    assert (bundles_dir / "my-bundle" / "bundle.yaml").is_file()


@pytest.mark.unit
def test_install_bundle_invalid_zip(tmp_path: Path, bundles_dir: Path) -> None:
    """A file that is not a valid ZIP raises BundleValidationError."""
    from common.bundle_errors import BundleValidationError
    from common.bundles import install_bundle

    bad_zip = tmp_path / "bad.zip"
    bad_zip.write_bytes(b"this is not a zip file at all")

    with pytest.raises(BundleValidationError, match="not a valid ZIP"):
        install_bundle(bad_zip, bundles_dir)


@pytest.mark.unit
def test_install_bundle_zip_bomb(tmp_path: Path, bundles_dir: Path) -> None:
    """ZIP with total uncompressed size > 50 MB raises BundleValidationError."""
    from common.bundle_errors import BundleValidationError
    from common.bundles import install_bundle

    # 51 MB uncompressed
    fifty_one_mb = 51 * 1024 * 1024
    zip_data = make_bundle_zip("bomb-bundle", total_size_bytes=fifty_one_mb)
    zip_path = tmp_path / "bomb.zip"
    zip_path.write_bytes(zip_data)

    with pytest.raises(BundleValidationError, match="exceeds.*50.*MB"):
        install_bundle(zip_path, bundles_dir)


@pytest.mark.unit
def test_install_bundle_path_traversal(tmp_path: Path, bundles_dir: Path) -> None:
    """ZIP member with path traversal (../) raises BundleValidationError."""
    from common.bundle_errors import BundleValidationError
    from common.bundles import install_bundle

    zip_data = make_bundle_zip(
        "traverse-bundle",
        extra_members=[("traverse-bundle/../../../etc/passwd", b"root:x:0:0")],
    )
    zip_path = tmp_path / "traverse.zip"
    zip_path.write_bytes(zip_data)

    with pytest.raises(BundleValidationError, match="path traversal"):
        install_bundle(zip_path, bundles_dir)


@pytest.mark.unit
def test_install_bundle_missing_manifest(tmp_path: Path, bundles_dir: Path) -> None:
    """ZIP without bundle.yaml raises BundleValidationError."""
    from common.bundle_errors import BundleValidationError
    from common.bundles import install_bundle

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("some-bundle/README.txt", "no manifest here")
    zip_path = tmp_path / "no_manifest.zip"
    zip_path.write_bytes(buf.getvalue())

    with pytest.raises(BundleValidationError, match="bundle.yaml"):
        install_bundle(zip_path, bundles_dir)


@pytest.mark.unit
def test_install_bundle_invalid_manifest_name(tmp_path: Path, bundles_dir: Path) -> None:
    """bundle.yaml with an invalid name raises BundleValidationError."""
    from common.bundle_errors import BundleValidationError
    from common.bundles import install_bundle

    # Names with uppercase letters, spaces, or leading digits are invalid
    zip_data = make_bundle_zip(
        "Invalid_Name",
        manifest_overrides={"name": "Invalid_Name"},
        root_dir_name="Invalid_Name",
    )
    zip_path = tmp_path / "bad_name.zip"
    zip_path.write_bytes(zip_data)

    with pytest.raises(BundleValidationError, match="invalid.*name"):
        install_bundle(zip_path, bundles_dir)


@pytest.mark.unit
def test_install_bundle_dir_mismatch(tmp_path: Path, bundles_dir: Path) -> None:
    """Root dir name != manifest name raises BundleValidationError."""
    from common.bundle_errors import BundleValidationError
    from common.bundles import install_bundle

    # ZIP root is "dir-one" but manifest says name is "dir-two"
    zip_data = make_bundle_zip(
        "dir-two",
        manifest_overrides={"name": "dir-two"},
        root_dir_name="dir-one",
    )
    zip_path = tmp_path / "mismatch.zip"
    zip_path.write_bytes(zip_data)

    with pytest.raises(BundleValidationError, match="mismatch"):
        install_bundle(zip_path, bundles_dir)


@pytest.mark.unit
def test_install_bundle_conflict_warning(
    tmp_path: Path, bundles_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Installing a bundle whose tool names clash with an existing bundle logs WARNING."""
    import logging

    from common.bundles import install_bundle

    # Install first bundle with tool "my-tool"
    zip1 = make_bundle_zip("bundle-alpha", manifest_overrides={"tools": ["my-tool"]})
    zip1_path = tmp_path / "alpha.zip"
    zip1_path.write_bytes(zip1)
    install_bundle(zip1_path, bundles_dir)

    # Install second bundle with the same tool name
    zip2 = make_bundle_zip("bundle-beta", manifest_overrides={"tools": ["my-tool"]})
    zip2_path = tmp_path / "beta.zip"
    zip2_path.write_bytes(zip2)

    with caplog.at_level(logging.WARNING):
        install_bundle(zip2_path, bundles_dir)

    assert any("my-tool" in record.message for record in caplog.records)
    assert any(record.levelno == logging.WARNING for record in caplog.records)


@pytest.mark.unit
def test_uninstall_bundle_success(tmp_path: Path, bundles_dir: Path) -> None:
    """Uninstalling a bundle removes its directory."""
    from common.bundles import install_bundle, uninstall_bundle

    zip_data = make_bundle_zip("remove-me")
    zip_path = tmp_path / "remove-me.zip"
    zip_path.write_bytes(zip_data)
    install_bundle(zip_path, bundles_dir)

    assert (bundles_dir / "remove-me").is_dir()

    uninstall_bundle("remove-me", bundles_dir)

    assert not (bundles_dir / "remove-me").exists()


@pytest.mark.unit
def test_uninstall_bundle_not_found(bundles_dir: Path) -> None:
    """Uninstalling a non-existent bundle raises BundleNotFoundError."""
    from common.bundle_errors import BundleNotFoundError
    from common.bundles import uninstall_bundle

    with pytest.raises(BundleNotFoundError, match="ghost-bundle"):
        uninstall_bundle("ghost-bundle", bundles_dir)


@pytest.mark.unit
def test_list_bundles_empty(bundles_dir: Path) -> None:
    """list_bundles returns an empty list when no bundles are installed."""
    from common.bundles import list_bundles

    result = list_bundles(bundles_dir)

    assert result == []


@pytest.mark.unit
def test_list_bundles_multiple(tmp_path: Path, bundles_dir: Path) -> None:
    """list_bundles returns a sorted list of BundleManifest for each installed bundle."""
    from common.bundles import BundleManifest, install_bundle, list_bundles

    for name in ("bundle-zzz", "bundle-aaa", "bundle-mmm"):
        zip_data = make_bundle_zip(name)
        zip_path = tmp_path / f"{name}.zip"
        zip_path.write_bytes(zip_data)
        install_bundle(zip_path, bundles_dir)

    result = list_bundles(bundles_dir)

    assert len(result) == 3
    assert all(isinstance(m, BundleManifest) for m in result)
    # Must be sorted by name
    assert [m.name for m in result] == ["bundle-aaa", "bundle-mmm", "bundle-zzz"]


@pytest.mark.unit
def test_bundle_manifest_from_yaml(tmp_path: Path) -> None:
    """load_bundle_manifest correctly parses a bundle.yaml file."""
    from common.bundles import BundleManifest, load_bundle_manifest

    bundle_dir = tmp_path / "my-bundle"
    bundle_dir.mkdir()
    manifest_data = {
        "name": "my-bundle",
        "description": "A great bundle",
        "version": "2.3.1",
        "author": "Alice",
        "tools": ["tool-a", "tool-b"],
    }
    (bundle_dir / "bundle.yaml").write_text(yaml.dump(manifest_data))

    manifest = load_bundle_manifest(bundle_dir)

    assert manifest is not None
    assert isinstance(manifest, BundleManifest)
    assert manifest.name == "my-bundle"
    assert manifest.description == "A great bundle"
    assert manifest.version == "2.3.1"
    assert manifest.author == "Alice"
    assert manifest.tools == ["tool-a", "tool-b"]


@pytest.mark.unit
def test_bundle_manifest_invalid_name() -> None:
    """BundleManifest rejects names that don't match [a-z0-9][a-z0-9-]*."""
    from common.bundle_errors import BundleValidationError
    from common.bundles import BundleManifest

    invalid_names = [
        "InvalidName",   # uppercase
        "-starts-with-dash",  # leading dash
        "has spaces",    # space
        "has_underscore",  # underscore
        "",              # empty
        "ALLCAPS",       # all uppercase
    ]

    for bad_name in invalid_names:
        with pytest.raises((BundleValidationError, ValueError), match=r"(?i)(invalid|name)"):
            BundleManifest(
                name=bad_name,
                description="desc",
                version="1.0.0",
                author="",
                tools=[],
            )


@pytest.mark.unit
def test_install_bundle_nonexistent_file(bundles_dir: Path) -> None:
    """install_bundle raises BundleValidationError when zip_path does not exist."""
    from common.bundle_errors import BundleValidationError
    from common.bundles import install_bundle

    with pytest.raises(BundleValidationError, match="not found"):
        install_bundle(Path("/nonexistent/path/bundle.zip"), bundles_dir)


@pytest.mark.unit
def test_install_bundle_replaces_existing(tmp_path: Path, bundles_dir: Path) -> None:
    """Re-installing a bundle with the same name replaces the existing installation."""
    from common.bundles import install_bundle

    # First install
    zip_data = make_bundle_zip("replaceable", manifest_overrides={"version": "1.0.0"})
    zip_path = tmp_path / "replaceable-v1.zip"
    zip_path.write_bytes(zip_data)
    manifest_v1 = install_bundle(zip_path, bundles_dir)
    assert manifest_v1.version == "1.0.0"

    # Second install with updated version
    zip_data2 = make_bundle_zip("replaceable", manifest_overrides={"version": "2.0.0"})
    zip_path2 = tmp_path / "replaceable-v2.zip"
    zip_path2.write_bytes(zip_data2)
    manifest_v2 = install_bundle(zip_path2, bundles_dir)
    assert manifest_v2.version == "2.0.0"

    # Only one directory should exist
    assert (bundles_dir / "replaceable").is_dir()
    installed = list((bundles_dir).iterdir())
    assert len(installed) == 1


@pytest.mark.unit
def test_list_bundles_skips_invalid_dirs(bundles_dir: Path) -> None:
    """list_bundles silently skips directories with invalid or missing bundle.yaml."""
    # Create a directory with no bundle.yaml
    (bundles_dir / "broken-bundle").mkdir()
    # Create a directory with malformed YAML
    bad_yaml_dir = bundles_dir / "malformed-bundle"
    bad_yaml_dir.mkdir()
    (bad_yaml_dir / "bundle.yaml").write_text(":::invalid yaml:::")

    from common.bundles import list_bundles

    result = list_bundles(bundles_dir)
    assert result == []


@pytest.mark.unit
def test_load_bundle_manifest_missing_returns_none(tmp_path: Path) -> None:
    """load_bundle_manifest returns None when bundle.yaml is absent."""
    from common.bundles import load_bundle_manifest

    bundle_dir = tmp_path / "no-manifest"
    bundle_dir.mkdir()

    result = load_bundle_manifest(bundle_dir)
    assert result is None


@pytest.mark.unit
def test_bundle_manifest_default_values(tmp_path: Path) -> None:
    """bundle.yaml with only required fields uses expected defaults."""
    from common.bundles import BundleManifest, load_bundle_manifest

    bundle_dir = tmp_path / "minimal-bundle"
    bundle_dir.mkdir()
    minimal = {"name": "minimal-bundle", "description": "Minimal"}
    (bundle_dir / "bundle.yaml").write_text(yaml.dump(minimal))

    manifest = load_bundle_manifest(bundle_dir)

    assert manifest is not None
    assert manifest.version == "1.0.0"
    assert manifest.author == ""
    assert manifest.tools == []
