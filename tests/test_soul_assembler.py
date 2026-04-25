"""Unit tests for atelier.soul_assembler.assemble_system_prompt — path validation design.

RED phase: Tests written before the new implementation.
New design: assemble_system_prompt validates paths and returns memory_paths (list of
absolute path strings), NOT file content. File reading is delegated to DeepAgents
via the memory= parameter.
"""

import pytest
from pathlib import Path

from atelier.soul_assembler import assemble_system_prompt, AssemblyResult


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


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


def _write(path: Path, content: str = "# content") -> None:
    """Write text to a file, creating parent directories as needed.

    Args:
        path: Absolute path of the file to create.
        content: Text content to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1 — AssemblyResult has memory_paths (not prompt)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_assembly_result_has_memory_paths_field() -> None:
    """AssemblyResult is a NamedTuple with memory_paths, issues, is_degraded fields.

    The old 'prompt' field must NOT exist. The new field is 'memory_paths'.
    """
    r = AssemblyResult(memory_paths=[], issues=[], is_degraded=False)
    assert r.memory_paths == []
    assert r.issues == []
    assert r.is_degraded is False


@pytest.mark.unit
def test_assembly_result_has_no_prompt_field() -> None:
    """AssemblyResult must not have a 'prompt' field — clean break from old design."""
    r = AssemblyResult(memory_paths=[], issues=[], is_degraded=False)
    assert not hasattr(r, "prompt"), (
        "AssemblyResult still has a 'prompt' field — old design not removed"
    )


# ---------------------------------------------------------------------------
# Test 2 — Valid paths within jail → included in memory_paths as absolute strings
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_valid_soul_path_included_in_memory_paths(tmp_path: Path) -> None:
    """soul/SOUL.md present → its absolute path appears in memory_paths.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    soul_file = prompts / "soul" / "SOUL.md"
    _write(soul_file)

    result = assemble_system_prompt(prompts)

    assert str(soul_file) in result.memory_paths


@pytest.mark.unit
def test_valid_role_path_included_in_memory_paths(tmp_path: Path) -> None:
    """A valid role_prompt_path is resolved and included in memory_paths.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md")
    role_file = prompts / "roles" / "admin.md"
    _write(role_file)

    result = assemble_system_prompt(prompts, role_prompt_path="roles/admin.md")

    assert str(role_file) in result.memory_paths


@pytest.mark.unit
def test_valid_user_path_included_in_memory_paths(tmp_path: Path) -> None:
    """A valid user_prompt_path is resolved and included in memory_paths.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md")
    user_file = prompts / "users" / "discord_123.md"
    _write(user_file)

    result = assemble_system_prompt(prompts, user_prompt_path="users/discord_123.md")

    assert str(user_file) in result.memory_paths


@pytest.mark.unit
def test_valid_channel_path_included_in_memory_paths(tmp_path: Path) -> None:
    """A valid channel_prompt_path is resolved and included in memory_paths.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md")
    channel_file = prompts / "channels" / "discord_default.md"
    _write(channel_file)

    result = assemble_system_prompt(prompts, channel_prompt_path="channels/discord_default.md")

    assert str(channel_file) in result.memory_paths


@pytest.mark.unit
def test_all_valid_paths_included_in_memory_paths(tmp_path: Path) -> None:
    """All four valid layers → all four absolute paths in memory_paths.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    soul_file = prompts / "soul" / "SOUL.md"
    role_file = prompts / "roles" / "admin.md"
    user_file = prompts / "users" / "discord_123.md"
    channel_file = prompts / "channels" / "discord_default.md"
    for f in [soul_file, role_file, user_file, channel_file]:
        _write(f)

    result = assemble_system_prompt(
        prompts,
        role_prompt_path="roles/admin.md",
        user_prompt_path="users/discord_123.md",
        channel_prompt_path="channels/discord_default.md",
    )

    assert str(soul_file) in result.memory_paths
    assert str(role_file) in result.memory_paths
    assert str(user_file) in result.memory_paths
    assert str(channel_file) in result.memory_paths
    assert len(result.memory_paths) == 4


# ---------------------------------------------------------------------------
# Test 3 — All returned paths are absolute strings
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_returned_paths_are_absolute_strings(tmp_path: Path) -> None:
    """Every path in memory_paths is an absolute string (not a Path object).

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md")
    _write(prompts / "roles" / "admin.md")

    result = assemble_system_prompt(prompts, role_prompt_path="roles/admin.md")

    for p in result.memory_paths:
        assert isinstance(p, str), f"Expected str, got {type(p)}: {p!r}"
        assert Path(p).is_absolute(), f"Expected absolute path, got: {p!r}"


# ---------------------------------------------------------------------------
# Test 4 — Missing files → excluded + is_degraded=True + message in issues
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_soul_excluded_is_degraded(tmp_path: Path) -> None:
    """soul/SOUL.md absent → not in memory_paths, is_degraded=True, issues non-empty.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    # soul/SOUL.md intentionally not created

    result = assemble_system_prompt(prompts)

    assert result.is_degraded is True
    assert len(result.issues) >= 1
    soul_path = str(prompts / "soul" / "SOUL.md")
    assert soul_path not in result.memory_paths


@pytest.mark.unit
def test_missing_role_file_excluded_is_degraded(tmp_path: Path) -> None:
    """Missing role file → not in memory_paths, is_degraded=True, path mentioned in issues.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md")
    # roles/admin.md intentionally not created

    result = assemble_system_prompt(prompts, role_prompt_path="roles/admin.md")

    assert result.is_degraded is True
    assert any("admin.md" in issue for issue in result.issues)
    # The missing path must not appear in memory_paths
    assert all("admin.md" not in p for p in result.memory_paths)


@pytest.mark.unit
def test_missing_channel_file_excluded_is_degraded(tmp_path: Path) -> None:
    """Missing channel file → not in memory_paths, is_degraded=True.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md")
    # channels/missing.md intentionally not created

    result = assemble_system_prompt(prompts, channel_prompt_path="channels/missing.md")

    assert result.is_degraded is True
    assert all("missing.md" not in p for p in result.memory_paths)


@pytest.mark.unit
def test_multiple_missing_files_all_in_issues(tmp_path: Path) -> None:
    """Multiple missing layers → all mentioned in issues, is_degraded=True.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md")
    # role and channel intentionally not created

    result = assemble_system_prompt(
        prompts,
        role_prompt_path="roles/missing_role.md",
        channel_prompt_path="channels/missing_ch.md",
    )

    assert result.is_degraded is True
    issue_text = " ".join(result.issues)
    assert "missing_role.md" in issue_text
    assert "missing_ch.md" in issue_text


# ---------------------------------------------------------------------------
# Test 5 — Path traversal → excluded + is_degraded=True + message in issues
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_path_traversal_via_dotdot_excluded(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Path traversal via '..' rejected: not in memory_paths, is_degraded=True.

    Args:
        tmp_path: pytest temporary directory.
        caplog: pytest log capture fixture.
    """
    import logging

    prompts = _make_prompts_dir(tmp_path)
    # create a file outside the prompts_dir
    secret_file = tmp_path / "secret.md"
    _write(secret_file, "SECRET CONTENT")

    with caplog.at_level(logging.WARNING):
        result = assemble_system_prompt(prompts, role_prompt_path="../secret.md")

    assert result.is_degraded is True
    assert str(secret_file) not in result.memory_paths
    assert any("escapes" in issue or "traversal" in issue.lower() for issue in result.issues)


@pytest.mark.unit
def test_absolute_path_argument_excluded(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Absolute path argument rejected: not in memory_paths, is_degraded=True.

    Args:
        tmp_path: pytest temporary directory.
        caplog: pytest log capture fixture.
    """
    import logging

    prompts = _make_prompts_dir(tmp_path)
    outside_file = tmp_path / "outside_prompts.md"
    _write(outside_file, "SHOULD NOT LOAD")

    with caplog.at_level(logging.WARNING):
        result = assemble_system_prompt(prompts, user_prompt_path=outside_file)

    assert result.is_degraded is True
    assert str(outside_file) not in result.memory_paths
    assert any("relative" in issue or "absolute" in issue.lower() for issue in result.issues)


# ---------------------------------------------------------------------------
# Test 6 — None path → ignored (no error, no entry)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_none_role_path_ignored(tmp_path: Path) -> None:
    """None role_prompt_path is silently ignored — no error, no extra entry.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md")

    result = assemble_system_prompt(prompts, role_prompt_path=None)

    assert result.is_degraded is False
    assert result.issues == []
    # Only the soul path should be present
    assert len(result.memory_paths) == 1


@pytest.mark.unit
def test_all_none_paths_no_errors(tmp_path: Path) -> None:
    """All optional paths None → no issues, no extra entries in memory_paths.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md")

    result = assemble_system_prompt(
        prompts,
        role_prompt_path=None,
        user_prompt_path=None,
        channel_prompt_path=None,
    )

    assert result.is_degraded is False
    assert result.issues == []


# ---------------------------------------------------------------------------
# Test 7 — Mix of valid and invalid → only valid in memory_paths, degraded=True
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mix_valid_and_invalid_only_valid_in_memory_paths(tmp_path: Path) -> None:
    """Valid paths included, invalid (missing) paths excluded, is_degraded=True.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    soul_file = prompts / "soul" / "SOUL.md"
    role_file = prompts / "roles" / "admin.md"
    _write(soul_file)
    _write(role_file)
    # channel intentionally not created

    result = assemble_system_prompt(
        prompts,
        role_prompt_path="roles/admin.md",
        channel_prompt_path="channels/missing.md",
    )

    assert result.is_degraded is True
    assert str(soul_file) in result.memory_paths
    assert str(role_file) in result.memory_paths
    assert len(result.memory_paths) == 2


@pytest.mark.unit
def test_mix_valid_and_traversal_only_valid_in_memory_paths(tmp_path: Path) -> None:
    """Valid paths included, traversal paths excluded, is_degraded=True.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    soul_file = prompts / "soul" / "SOUL.md"
    _write(soul_file)
    # Create a file outside the jail
    secret = tmp_path / "secret.md"
    _write(secret)

    result = assemble_system_prompt(
        prompts,
        role_prompt_path="../secret.md",
    )

    assert result.is_degraded is True
    assert str(soul_file) in result.memory_paths
    assert str(secret) not in result.memory_paths
    assert len(result.memory_paths) == 1


# ---------------------------------------------------------------------------
# Test 8 — No files at all → empty memory_paths, degraded (soul missing)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_layers_returns_empty_memory_paths(tmp_path: Path) -> None:
    """No soul file and no optional args → empty memory_paths, is_degraded=True.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)

    result = assemble_system_prompt(prompts)

    assert result.memory_paths == []
    assert result.is_degraded is True


# ---------------------------------------------------------------------------
# Test 9 — Order preservation in memory_paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_memory_paths_order_soul_role_user_channel(tmp_path: Path) -> None:
    """memory_paths preserves layer order: soul, role, user, channel.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    soul_file = prompts / "soul" / "SOUL.md"
    role_file = prompts / "roles" / "admin.md"
    user_file = prompts / "users" / "u.md"
    channel_file = prompts / "channels" / "ch.md"
    for f in [soul_file, role_file, user_file, channel_file]:
        _write(f)

    result = assemble_system_prompt(
        prompts,
        role_prompt_path="roles/admin.md",
        user_prompt_path="users/u.md",
        channel_prompt_path="channels/ch.md",
    )

    assert result.memory_paths == [
        str(soul_file),
        str(role_file),
        str(user_file),
        str(channel_file),
    ]


# ---------------------------------------------------------------------------
# Test 10 — not_degraded when all present
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_files_present_not_degraded(tmp_path: Path) -> None:
    """All layers present → is_degraded=False, issues empty.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md")
    _write(prompts / "roles" / "admin.md")
    _write(prompts / "users" / "u.md")
    _write(prompts / "channels" / "ch.md")

    result = assemble_system_prompt(
        prompts,
        role_prompt_path="roles/admin.md",
        user_prompt_path="users/u.md",
        channel_prompt_path="channels/ch.md",
    )

    assert isinstance(result, AssemblyResult)
    assert result.is_degraded is False
    assert result.issues == []
    assert len(result.memory_paths) == 4
