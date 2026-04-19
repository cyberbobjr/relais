"""Tests for the RELAIS CLI bundle subcommands — Phase 3.

TDD: tests written FIRST before implementation.
All tests use tmp_path + monkeypatch to isolate from real filesystem.
capsys is used to capture stdout/stderr output.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers — build synthetic ZIP bytes in memory
# ---------------------------------------------------------------------------


def make_bundle_zip(
    bundle_name: str,
    manifest_overrides: dict | None = None,
) -> bytes:
    """Build a valid (or intentionally broken) bundle ZIP in memory.

    Args:
        bundle_name: Name used in bundle.yaml ``name`` field and as root dir.
        manifest_overrides: Dict merged into the default manifest YAML; use
            ``None`` values to delete a key.

    Returns:
        Raw ZIP bytes.
    """
    manifest: dict = {
        "name": bundle_name,
        "description": f"Test bundle {bundle_name}",
        "version": "1.2.3",
        "author": "tester",
        "tools": [],
    }
    if manifest_overrides:
        for k, v in manifest_overrides.items():
            if v is None:
                manifest.pop(k, None)
            else:
                manifest[k] = v

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{bundle_name}/bundle.yaml", yaml.dump(manifest))
        zf.writestr(f"{bundle_name}/README.md", "# Test bundle")
    return buf.getvalue()


def write_bundle_zip(tmp_path: Path, bundle_name: str, **manifest_overrides) -> Path:
    """Write a bundle ZIP to tmp_path and return its path.

    Args:
        tmp_path: Pytest tmp_path fixture directory.
        bundle_name: Bundle name and root dir inside ZIP.
        **manifest_overrides: Keyword overrides applied to the manifest dict.

    Returns:
        Path to the written ZIP file.
    """
    data = make_bundle_zip(bundle_name, manifest_overrides or None)
    zip_path = tmp_path / f"{bundle_name}.zip"
    zip_path.write_bytes(data)
    return zip_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bundles_dir(tmp_path: Path) -> Path:
    """An isolated, empty bundles directory.

    Returns:
        Path to the created bundles directory.
    """
    d = tmp_path / "bundles"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# cmd_install
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bundle_install_success(
    tmp_path: Path, bundles_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    """cmd_install prints success message and returns 0 for a valid ZIP.

    Args:
        tmp_path: Pytest tmp_path fixture.
        bundles_dir: Isolated bundles directory.
        capsys: Pytest stdout/stderr capture fixture.
    """
    from relais_tui.cli.bundle import cmd_install

    zip_path = write_bundle_zip(tmp_path, "my-bundle")

    class Args:
        zip_file = str(zip_path)

    with patch("relais_tui.cli.bundle.get_bundles_dir", return_value=bundles_dir):
        result = cmd_install(Args())

    assert result == 0
    out = capsys.readouterr().out
    assert "my-bundle" in out
    assert "1.2.3" in out


@pytest.mark.unit
def test_bundle_install_not_found(
    tmp_path: Path, bundles_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    """cmd_install returns 1 and prints error when zip_file path does not exist.

    Args:
        tmp_path: Pytest tmp_path fixture.
        bundles_dir: Isolated bundles directory.
        capsys: Pytest stdout/stderr capture fixture.
    """
    from relais_tui.cli.bundle import cmd_install

    class Args:
        zip_file = str(tmp_path / "nonexistent.zip")

    with patch("relais_tui.cli.bundle.get_bundles_dir", return_value=bundles_dir):
        result = cmd_install(Args())

    assert result == 1
    out = capsys.readouterr().out
    assert "error" in out.lower() or "Error" in out


@pytest.mark.unit
def test_bundle_install_validation_error(
    tmp_path: Path, bundles_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    """cmd_install returns 1 and prints error for an invalid (not-a-ZIP) file.

    Args:
        tmp_path: Pytest tmp_path fixture.
        bundles_dir: Isolated bundles directory.
        capsys: Pytest stdout/stderr capture fixture.
    """
    from relais_tui.cli.bundle import cmd_install

    bad_zip = tmp_path / "bad.zip"
    bad_zip.write_bytes(b"this is not a zip file")

    class Args:
        zip_file = str(bad_zip)

    with patch("relais_tui.cli.bundle.get_bundles_dir", return_value=bundles_dir):
        result = cmd_install(Args())

    assert result == 1
    out = capsys.readouterr().out
    assert "error" in out.lower() or "Error" in out


@pytest.mark.unit
def test_bundle_install_no_stack_trace(
    tmp_path: Path, bundles_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    """cmd_install does not print a Python traceback on error.

    Args:
        tmp_path: Pytest tmp_path fixture.
        bundles_dir: Isolated bundles directory.
        capsys: Pytest stdout/stderr capture fixture.
    """
    from relais_tui.cli.bundle import cmd_install

    class Args:
        zip_file = str(tmp_path / "nonexistent.zip")

    with patch("relais_tui.cli.bundle.get_bundles_dir", return_value=bundles_dir):
        cmd_install(Args())

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Traceback" not in combined
    assert "raise " not in combined


# ---------------------------------------------------------------------------
# cmd_uninstall
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bundle_uninstall_success(
    tmp_path: Path, bundles_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    """cmd_uninstall removes an installed bundle and returns 0.

    Args:
        tmp_path: Pytest tmp_path fixture.
        bundles_dir: Isolated bundles directory.
        capsys: Pytest stdout/stderr capture fixture.
    """
    from relais_tui.cli.bundle import cmd_uninstall

    # Pre-install a bundle by creating its directory + bundle.yaml
    bundle_dir = bundles_dir / "my-bundle"
    bundle_dir.mkdir()
    (bundle_dir / "bundle.yaml").write_text(
        "name: my-bundle\ndescription: A test bundle\nversion: 1.0.0\n"
    )

    class Args:
        name = "my-bundle"

    with patch("relais_tui.cli.bundle.get_bundles_dir", return_value=bundles_dir):
        result = cmd_uninstall(Args())

    assert result == 0
    out = capsys.readouterr().out
    assert "my-bundle" in out
    assert not (bundles_dir / "my-bundle").exists()


@pytest.mark.unit
def test_bundle_uninstall_not_found(
    bundles_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    """cmd_uninstall returns 1 and prints an error when bundle is not installed.

    Args:
        bundles_dir: Isolated bundles directory.
        capsys: Pytest stdout/stderr capture fixture.
    """
    from relais_tui.cli.bundle import cmd_uninstall

    class Args:
        name = "ghost-bundle"

    with patch("relais_tui.cli.bundle.get_bundles_dir", return_value=bundles_dir):
        result = cmd_uninstall(Args())

    assert result == 1
    out = capsys.readouterr().out
    assert "error" in out.lower() or "Error" in out


@pytest.mark.unit
def test_bundle_uninstall_no_stack_trace(
    bundles_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    """cmd_uninstall does not print a Python traceback on error.

    Args:
        bundles_dir: Isolated bundles directory.
        capsys: Pytest stdout/stderr capture fixture.
    """
    from relais_tui.cli.bundle import cmd_uninstall

    class Args:
        name = "ghost-bundle"

    with patch("relais_tui.cli.bundle.get_bundles_dir", return_value=bundles_dir):
        cmd_uninstall(Args())

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Traceback" not in combined


# ---------------------------------------------------------------------------
# cmd_list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bundle_list_empty(
    bundles_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    """cmd_list prints 'No bundles installed.' when the bundles dir is empty.

    Args:
        bundles_dir: Isolated bundles directory.
        capsys: Pytest stdout/stderr capture fixture.
    """
    from relais_tui.cli.bundle import cmd_list

    class Args:
        pass

    with patch("relais_tui.cli.bundle.get_bundles_dir", return_value=bundles_dir):
        result = cmd_list(Args())

    assert result == 0
    out = capsys.readouterr().out
    assert "No bundles installed." in out


@pytest.mark.unit
def test_bundle_list_multiple(
    bundles_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    """cmd_list prints one line per bundle with name, version, and description.

    Args:
        bundles_dir: Isolated bundles directory.
        capsys: Pytest stdout/stderr capture fixture.
    """
    from relais_tui.cli.bundle import cmd_list

    # Install two bundles by creating their directories + bundle.yaml.
    # The beta-bundle description intentionally spans multiple lines; only the
    # first line should appear in cmd_list output.
    bundles_data = [
        ("alpha-bundle", "1.0.0", "First bundle"),
        ("beta-bundle", "2.1.0", "Second bundle\nwith multiline description"),
    ]
    for name, version, desc in bundles_data:
        d = bundles_dir / name
        d.mkdir()
        import yaml as _yaml
        (d / "bundle.yaml").write_text(
            _yaml.dump({"name": name, "description": desc, "version": version})
        )

    class Args:
        pass

    with patch("relais_tui.cli.bundle.get_bundles_dir", return_value=bundles_dir):
        result = cmd_list(Args())

    assert result == 0
    out = capsys.readouterr().out
    # Both bundle names must appear
    assert "alpha-bundle" in out
    assert "beta-bundle" in out
    # Versions must appear
    assert "1.0.0" in out
    assert "2.1.0" in out
    # Only first line of description
    assert "First bundle" in out
    assert "Second bundle" in out
    # Multiline second line must NOT appear
    assert "with multiline description" not in out


@pytest.mark.unit
def test_bundle_list_dir_nonexistent(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """cmd_list treats a non-existent bundles dir as empty.

    Args:
        tmp_path: Pytest tmp_path fixture.
        capsys: Pytest stdout/stderr capture fixture.
    """
    from relais_tui.cli.bundle import cmd_list

    nonexistent = tmp_path / "no-such-dir"

    class Args:
        pass

    with patch("relais_tui.cli.bundle.get_bundles_dir", return_value=nonexistent):
        result = cmd_list(Args())

    assert result == 0
    out = capsys.readouterr().out
    assert "No bundles installed." in out


# ---------------------------------------------------------------------------
# add_bundle_subparser + main integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_bundle_subparser_registers_subcommands() -> None:
    """add_bundle_subparser registers install, uninstall, list sub-subcommands.

    Verifies the parser structure without invoking any real commands.
    """
    import argparse

    from relais_tui.cli.bundle import add_bundle_subparser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    add_bundle_subparser(subparsers)

    # Parse each sub-subcommand to verify they are registered
    args = parser.parse_args(["bundle", "list"])
    assert hasattr(args, "func")

    args = parser.parse_args(["bundle", "install", "some/path.zip"])
    assert hasattr(args, "func")
    assert args.zip_file == "some/path.zip"

    args = parser.parse_args(["bundle", "uninstall", "my-bundle"])
    assert hasattr(args, "func")
    assert args.name == "my-bundle"


@pytest.mark.unit
def test_main_bundle_list_integration(
    bundles_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    """relais bundle list dispatches correctly through the argparse layer.

    Uses the argparse structure directly (avoids importing the Textual __main__
    module which requires the textual package outside the TUI venv).

    Args:
        bundles_dir: Isolated bundles directory.
        capsys: Pytest stdout/stderr capture fixture.
    """
    import argparse
    from unittest.mock import patch as _patch

    from relais_tui.cli.bundle import add_bundle_subparser

    root = argparse.ArgumentParser(prog="relais")
    root_sub = root.add_subparsers(dest="command")
    root_sub.required = True
    add_bundle_subparser(root_sub)

    with _patch("sys.argv", ["relais", "bundle", "list"]), _patch(
        "relais_tui.cli.bundle.get_bundles_dir", return_value=bundles_dir
    ):
        args = root.parse_args()
        try:
            result = args.func(args)
        except SystemExit as exc:
            assert exc.code == 0 or exc.code is None
            result = 0

    assert result == 0
    out = capsys.readouterr().out
    assert "No bundles installed." in out
