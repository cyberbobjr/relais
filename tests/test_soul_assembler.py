"""Unit tests for atelier.soul_assembler.assemble_system_prompt.

Tests follow TDD: written before the implementation, verified RED first.
"""

import pytest
from pathlib import Path

from atelier.soul_assembler import assemble_system_prompt


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    """Write text to a file, creating parent directories as needed.

    Args:
        path: Absolute path of the file to create.
        content: Text content to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_prompts_dir(tmp_path: Path) -> Path:
    """Return a prompts_dir skeleton (no files created).

    Args:
        tmp_path: pytest tmp_path fixture value.

    Returns:
        Path to the empty prompts directory.
    """
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    return prompts


# ---------------------------------------------------------------------------
# Test 1 – soul only
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_soul_only(tmp_path: Path) -> None:
    """Only soul/SOUL.md present — result equals its content verbatim.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    soul_content = "# SOUL\nYou are RELAIS."
    _write(prompts / "soul" / "SOUL.md", soul_content)

    result = assemble_system_prompt(prompts)

    assert result == soul_content


# ---------------------------------------------------------------------------
# Test 2 – all layers assembled in correct order with separators
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_layers_assembled_in_order(tmp_path: Path) -> None:
    """All layer files present — result contains all layers in order with separators.

    Layer order: soul → role → user → channel.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md", "SOUL")
    _write(prompts / "roles" / "admin.md", "ROLE")
    _write(prompts / "users" / "discord_123.md", "USER")
    _write(prompts / "channels" / "telegram_default.md", "CHANNEL")

    result = assemble_system_prompt(
        prompts,
        channel="telegram",
        user_prompt_path=prompts / "users" / "discord_123.md",
        user_role="admin",
    )

    sep = "\n\n---\n\n"
    expected = sep.join(["SOUL", "ROLE", "USER", "CHANNEL"])
    assert result == expected


# ---------------------------------------------------------------------------
# Test 3 – missing soul returns other layers without leading separator
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_soul_returns_other_layers(tmp_path: Path) -> None:
    """Soul file absent — result starts with the role layer, no leading separator.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "roles" / "user.md", "ROLE_CONTENT")

    result = assemble_system_prompt(prompts, user_role="user")

    assert result == "ROLE_CONTENT"
    assert not result.startswith("\n\n---\n\n")


# ---------------------------------------------------------------------------
# Test 4 – missing layer file silently skipped
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_layer_skipped_silently(tmp_path: Path) -> None:
    """Role file missing — no error raised, role content absent from output.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md", "SOUL_CONTENT")
    # roles/admin.md intentionally not created

    result = assemble_system_prompt(prompts, user_role="admin")

    assert result == "SOUL_CONTENT"
    assert "admin" not in result


# ---------------------------------------------------------------------------
# Test 5 – user_prompt_path loads the explicit file
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_user_prompt_path_loads_explicit_file(tmp_path: Path) -> None:
    """user_prompt_path loads the file at the given path (Layer 3).

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    user_file = tmp_path / "my_custom_prompt.md"
    user_file.write_text("USER_PROFILE", encoding="utf-8")

    result = assemble_system_prompt(prompts, user_prompt_path=user_file)

    assert result == "USER_PROFILE"


# ---------------------------------------------------------------------------
# Test 6 – no layers returns empty string
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_layers_returns_empty_string(tmp_path: Path) -> None:
    """No files — returns empty string without error.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)

    result = assemble_system_prompt(prompts)

    assert result == ""


# ---------------------------------------------------------------------------
# Test 7 – empty file treated as missing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_file_skipped(tmp_path: Path) -> None:
    """Layer file exists but is empty — treated as missing, not included in output.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md", "")
    _write(prompts / "roles" / "user.md", "ROLE_TEXT")

    result = assemble_system_prompt(prompts, user_role="user")

    assert result == "ROLE_TEXT"
    # Empty soul file must not appear at all, even as an empty separator fragment
    assert result.startswith("ROLE_TEXT")
