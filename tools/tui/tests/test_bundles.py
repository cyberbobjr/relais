"""Tests for relais_tui.bundles — TDD RED phase.

All tests in this file target the bundle management functions defined in
``relais_tui.bundles``.  External filesystem operations use ``tmp_path``; ZIP
internals are mocked where needed to test security edge cases without
constructing actual large archives.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from relais_tui.bundles import BundleInfo, get_bundles_dir, install_bundle, list_bundles, uninstall_bundle


# ---------------------------------------------------------------------------
# list_bundles
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_bundles_empty(tmp_path: Path) -> None:
    """An empty bundles directory returns an empty list."""
    result = list_bundles(bundles_dir=tmp_path)
    assert result == []


@pytest.mark.unit
def test_list_bundles_one_bundle(tmp_path: Path) -> None:
    """A directory with one valid bundle.yaml is returned as a BundleInfo."""
    bundle_dir = tmp_path / "my-bundle"
    bundle_dir.mkdir()
    manifest = {
        "name": "my-bundle",
        "description": "A test bundle.",
        "version": "1.2.3",
        "author": "Test Author",
    }
    (bundle_dir / "bundle.yaml").write_text(yaml.dump(manifest))

    result = list_bundles(bundles_dir=tmp_path)

    assert len(result) == 1
    info = result[0]
    assert info.name == "my-bundle"
    assert info.description == "A test bundle."
    assert info.version == "1.2.3"
    assert info.author == "Test Author"


@pytest.mark.unit
def test_list_bundles_skips_invalid_yaml(tmp_path: Path) -> None:
    """A bundle.yaml with invalid YAML syntax is silently skipped."""
    bundle_dir = tmp_path / "broken-bundle"
    bundle_dir.mkdir()
    (bundle_dir / "bundle.yaml").write_text("name: [unclosed bracket")

    result = list_bundles(bundles_dir=tmp_path)

    assert result == []


@pytest.mark.unit
def test_list_bundles_sorted(tmp_path: Path) -> None:
    """Returned bundles are sorted alphabetically by name."""
    for name in ("zebra-bundle", "alpha-bundle", "middle-bundle"):
        d = tmp_path / name
        d.mkdir()
        (d / "bundle.yaml").write_text(
            yaml.dump({"name": name, "description": "desc", "version": "1.0.0", "author": ""})
        )

    result = list_bundles(bundles_dir=tmp_path)

    assert [b.name for b in result] == ["alpha-bundle", "middle-bundle", "zebra-bundle"]


@pytest.mark.unit
def test_list_bundles_skips_files(tmp_path: Path) -> None:
    """Regular files at the bundles_dir root are not scanned."""
    (tmp_path / "not-a-dir.yaml").write_text("name: fake")

    result = list_bundles(bundles_dir=tmp_path)

    assert result == []


# ---------------------------------------------------------------------------
# install_bundle
# ---------------------------------------------------------------------------


def _make_zip(
    bundle_name: str,
    manifest: dict | None = None,
    extra_members: list[tuple[str, bytes]] | None = None,
) -> bytes:
    """Build a valid bundle ZIP in memory.

    Args:
        bundle_name: Root folder name (also used as bundle name in manifest).
        manifest: Override the auto-generated bundle.yaml content.
        extra_members: Additional (arcname, data) tuples to add to the ZIP.

    Returns:
        Raw ZIP bytes.
    """
    buf = io.BytesIO()
    default_manifest = {
        "name": bundle_name,
        "description": "A test bundle.",
        "version": "1.0.0",
        "author": "Tester",
    }
    manifest_data = yaml.dump(manifest if manifest is not None else default_manifest).encode()

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(f"{bundle_name}/bundle.yaml", manifest_data)
        if extra_members:
            for arcname, data in extra_members:
                zf.writestr(arcname, data)

    return buf.getvalue()


@pytest.mark.unit
def test_install_bundle_success(tmp_path: Path) -> None:
    """Installing a valid ZIP creates the bundle directory and returns BundleInfo."""
    zip_data = _make_zip("my-bundle")
    zip_path = tmp_path / "my-bundle.zip"
    zip_path.write_bytes(zip_data)

    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir()

    info = install_bundle(zip_path, bundles_dir=bundles_dir)

    assert info.name == "my-bundle"
    assert info.version == "1.0.0"
    assert info.author == "Tester"
    assert (bundles_dir / "my-bundle" / "bundle.yaml").exists()


@pytest.mark.unit
def test_install_bundle_zip_bomb(tmp_path: Path) -> None:
    """A ZIP whose total uncompressed size exceeds 50 MB raises ValueError."""
    zip_path = tmp_path / "bomb.zip"

    # Build a ZIP whose members report large uncompressed sizes via mocked ZipInfo.
    large_info = MagicMock()
    large_info.filename = "big-bundle/bundle.yaml"
    large_info.file_size = 60 * 1024 * 1024  # 60 MB

    manifest_bytes = yaml.dump(
        {"name": "big-bundle", "description": "desc", "version": "1.0.0", "author": ""}
    ).encode()

    with patch("relais_tui.bundles.zipfile.ZipFile") as mock_zf_class:
        mock_zf = MagicMock()
        mock_zf.__enter__ = lambda s: s
        mock_zf.__exit__ = MagicMock(return_value=False)
        mock_zf.infolist.return_value = [large_info]
        mock_zf.read.return_value = manifest_bytes
        mock_zf_class.return_value = mock_zf

        zip_path.write_bytes(b"fake")

        with pytest.raises(ValueError, match="ZIP bomb"):
            install_bundle(zip_path, bundles_dir=tmp_path / "bundles")


@pytest.mark.unit
def test_install_bundle_path_traversal(tmp_path: Path) -> None:
    """A ZIP containing a path-traversal member raises ValueError."""
    buf = io.BytesIO()
    manifest = {
        "name": "evil-bundle",
        "description": "desc",
        "version": "1.0.0",
        "author": "",
    }
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("evil-bundle/bundle.yaml", yaml.dump(manifest))
        zf.writestr("evil-bundle/../../../etc/passwd", "root:x:0:0")

    zip_path = tmp_path / "evil.zip"
    zip_path.write_bytes(buf.getvalue())

    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir()

    with pytest.raises(ValueError, match="[Pp]ath traversal"):
        install_bundle(zip_path, bundles_dir=bundles_dir)


@pytest.mark.unit
def test_install_bundle_missing_manifest(tmp_path: Path) -> None:
    """A ZIP without bundle.yaml raises ValueError."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("no-manifest-bundle/README.txt", "hello")

    zip_path = tmp_path / "no_manifest.zip"
    zip_path.write_bytes(buf.getvalue())

    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir()

    with pytest.raises(ValueError, match="bundle.yaml"):
        install_bundle(zip_path, bundles_dir=bundles_dir)


@pytest.mark.unit
def test_install_bundle_not_found(tmp_path: Path) -> None:
    """A non-existent ZIP path raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        install_bundle(tmp_path / "nonexistent.zip", bundles_dir=tmp_path / "bundles")


@pytest.mark.unit
def test_install_bundle_replaces_existing(tmp_path: Path) -> None:
    """Re-installing a bundle replaces the existing directory atomically."""
    zip_data = _make_zip("my-bundle")
    zip_path = tmp_path / "my-bundle.zip"
    zip_path.write_bytes(zip_data)

    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir()

    # First install
    install_bundle(zip_path, bundles_dir=bundles_dir)
    # Second install should not raise
    info = install_bundle(zip_path, bundles_dir=bundles_dir)
    assert info.name == "my-bundle"


# ---------------------------------------------------------------------------
# uninstall_bundle
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_uninstall_bundle_success(tmp_path: Path) -> None:
    """Uninstalling an installed bundle removes its directory."""
    bundle_dir = tmp_path / "my-bundle"
    bundle_dir.mkdir()
    (bundle_dir / "bundle.yaml").write_text("name: my-bundle")

    uninstall_bundle("my-bundle", bundles_dir=tmp_path)

    assert not bundle_dir.exists()


@pytest.mark.unit
def test_uninstall_bundle_not_found(tmp_path: Path) -> None:
    """Attempting to uninstall a bundle that is not installed raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        uninstall_bundle("nonexistent-bundle", bundles_dir=tmp_path)


# ---------------------------------------------------------------------------
# get_bundles_dir
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_bundles_dir_creates_directory(tmp_path: Path) -> None:
    """get_bundles_dir() creates the directory if it does not exist."""
    with patch.dict("os.environ", {"RELAIS_HOME": str(tmp_path)}):
        bundles_dir = get_bundles_dir()

    assert bundles_dir.is_dir()
    assert bundles_dir == tmp_path / "bundles"


@pytest.mark.unit
def test_get_bundles_dir_fallback(tmp_path: Path) -> None:
    """get_bundles_dir() falls back to ~/.relais/bundles when RELAIS_HOME is unset."""
    env = {k: v for k, v in __import__("os").environ.items() if k != "RELAIS_HOME"}
    with patch.dict("os.environ", env, clear=True):
        bundles_dir = get_bundles_dir()

    expected = Path.home() / ".relais" / "bundles"
    assert bundles_dir == expected
