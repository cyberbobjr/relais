"""Unit tests for atelier.soul_assembler.assemble_system_prompt.

Tests follow TDD: written before the implementation, verified RED first.
"""

import pytest
from pathlib import Path

from atelier.soul_assembler import assemble_system_prompt, AssemblyResult


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

    assert result.prompt == soul_content


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
    _write(prompts / "channels" / "discord_default.md", "CHANNEL")

    result = assemble_system_prompt(
        prompts,
        role_prompt_path="roles/admin.md",
        user_prompt_path="users/discord_123.md",
        channel_prompt_path="channels/discord_default.md",
    )

    sep = "\n\n---\n\n"
    expected = sep.join(["SOUL", "ROLE", "USER", "CHANNEL"])
    assert result.prompt == expected


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

    result = assemble_system_prompt(prompts, role_prompt_path="roles/user.md")

    assert result.prompt == "ROLE_CONTENT"
    assert not result.prompt.startswith("\n\n---\n\n")


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

    result = assemble_system_prompt(prompts, role_prompt_path="roles/admin.md")

    assert result.prompt == "SOUL_CONTENT"
    assert "admin" not in result.prompt


# ---------------------------------------------------------------------------
# Test 5 – user_prompt_path loads the explicit file
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_user_prompt_path_loads_relative_file(tmp_path: Path) -> None:
    """user_prompt_path loads a relative path resolved against prompts_dir.

    The path must be relative to prompts_dir; absolute paths are rejected.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "users" / "discord_123.md", "USER_PROFILE")

    result = assemble_system_prompt(prompts, user_prompt_path="users/discord_123.md")

    assert result.prompt == "USER_PROFILE"


@pytest.mark.unit
def test_user_prompt_path_rejects_absolute_path(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """user_prompt_path rejects absolute paths with a warning and skips the layer.

    Args:
        tmp_path: pytest temporary directory.
        caplog: pytest log capture fixture.
    """
    import logging

    prompts = _make_prompts_dir(tmp_path)
    absolute_path = tmp_path / "outside_prompts.md"
    absolute_path.write_text("SHOULD NOT LOAD", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        result = assemble_system_prompt(prompts, user_prompt_path=absolute_path)

    assert result.prompt == ""
    assert any("must be relative" in r.message for r in caplog.records)


@pytest.mark.unit
def test_user_prompt_path_rejects_traversal(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """user_prompt_path rejects paths that escape prompts_dir via '..'.

    Args:
        tmp_path: pytest temporary directory.
        caplog: pytest log capture fixture.
    """
    import logging

    prompts = _make_prompts_dir(tmp_path)
    escape_target = tmp_path / "secret.md"
    escape_target.write_text("SECRET", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        result = assemble_system_prompt(prompts, user_prompt_path="../secret.md")

    assert result.prompt == ""
    assert any("escapes prompts_dir" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Test 5b – channel_prompt_path loads the explicit file
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_channel_prompt_path_loads_explicit_file(tmp_path: Path) -> None:
    """channel_prompt_path loads a relative path resolved against prompts_dir.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md", "SOUL")
    _write(prompts / "channels" / "discord_default.md", "CHANNEL_CONTENT")

    result = assemble_system_prompt(
        prompts,
        channel_prompt_path="channels/discord_default.md",
    )

    assert "CHANNEL_CONTENT" in result.prompt


@pytest.mark.unit
def test_channel_prompt_path_rejects_absolute_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """channel_prompt_path rejects absolute paths with a warning.

    Args:
        tmp_path: pytest temporary directory.
        caplog: pytest log capture fixture.
    """
    import logging

    prompts = _make_prompts_dir(tmp_path)
    absolute_path = tmp_path / "outside.md"
    absolute_path.write_text("SHOULD NOT LOAD", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        result = assemble_system_prompt(prompts, channel_prompt_path=absolute_path)

    assert "SHOULD NOT LOAD" not in result.prompt
    assert any("must be relative" in r.message for r in caplog.records)


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

    assert result.prompt == ""


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

    result = assemble_system_prompt(prompts, role_prompt_path="roles/user.md")

    assert result.prompt == "ROLE_TEXT"
    # Empty soul file must not appear at all, even as an empty separator fragment
    assert result.prompt.startswith("ROLE_TEXT")


# ---------------------------------------------------------------------------
# F-07 — AssemblyResult: degradation tracking
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_assembly_result_is_namedtuple() -> None:
    """AssemblyResult is a NamedTuple with prompt, issues, is_degraded fields."""
    r = AssemblyResult(prompt="p", issues=[], is_degraded=False)
    assert r.prompt == "p"
    assert r.issues == []
    assert r.is_degraded is False


@pytest.mark.unit
def test_all_files_present_not_degraded(tmp_path: Path) -> None:
    """All layers present → is_degraded=False, issues empty, prompt contains all layers.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md", "SOUL")
    _write(prompts / "roles" / "admin.md", "ROLE")
    _write(prompts / "users" / "u.md", "USER")
    _write(prompts / "channels" / "ch.md", "CHANNEL")

    result = assemble_system_prompt(
        prompts,
        role_prompt_path="roles/admin.md",
        user_prompt_path="users/u.md",
        channel_prompt_path="channels/ch.md",
    )

    assert isinstance(result, AssemblyResult)
    assert result.is_degraded is False
    assert result.issues == []
    sep = "\n\n---\n\n"
    assert result.prompt == sep.join(["SOUL", "ROLE", "USER", "CHANNEL"])


@pytest.mark.unit
def test_optional_layer_missing_is_degraded(tmp_path: Path) -> None:
    """Optional layer file missing → is_degraded=True, issues contains the path.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md", "SOUL")
    # roles/admin.md intentionally not created

    result = assemble_system_prompt(prompts, role_prompt_path="roles/admin.md")

    assert isinstance(result, AssemblyResult)
    assert result.is_degraded is True
    assert any("admin.md" in issue for issue in result.issues)
    assert result.prompt == "SOUL"


@pytest.mark.unit
def test_soul_missing_is_degraded(tmp_path: Path) -> None:
    """SOUL.md missing → is_degraded=True, issues non-empty, prompt is empty string.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    # soul/SOUL.md intentionally not created

    result = assemble_system_prompt(prompts)

    assert isinstance(result, AssemblyResult)
    assert result.is_degraded is True
    assert len(result.issues) >= 1
    assert result.prompt == ""


@pytest.mark.unit
def test_multiple_missing_layers_all_in_issues(tmp_path: Path) -> None:
    """Multiple layers missing → all appear in issues, is_degraded=True.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md", "SOUL")
    # role and channel intentionally not created

    result = assemble_system_prompt(
        prompts,
        role_prompt_path="roles/missing.md",
        channel_prompt_path="channels/missing.md",
    )

    assert isinstance(result, AssemblyResult)
    assert result.is_degraded is True
    assert len(result.issues) == 2
    issue_text = " ".join(result.issues)
    assert "missing.md" in issue_text


@pytest.mark.unit
def test_result_prompt_field_equals_old_string_return(tmp_path: Path) -> None:
    """result.prompt equals the string that assemble_system_prompt previously returned.

    Backward-compatibility check: callers that switched from the plain-str return
    to result.prompt must get exactly the same string.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md", "SOUL")
    _write(prompts / "roles" / "r.md", "ROLE")

    result = assemble_system_prompt(prompts, role_prompt_path="roles/r.md")

    sep = "\n\n---\n\n"
    assert result.prompt == sep.join(["SOUL", "ROLE"])
