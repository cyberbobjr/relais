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

    Layer order: soul → role → user → channel → policy → facts.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md", "SOUL")
    _write(prompts / "roles" / "admin.md", "ROLE")
    _write(prompts / "users" / "discord_123.md", "USER")
    _write(prompts / "channels" / "telegram_default.md", "CHANNEL")
    _write(prompts / "policies" / "in_meeting.md", "POLICY")

    result = assemble_system_prompt(
        prompts,
        channel="telegram",
        sender_id="discord:123",
        user_role="admin",
        reply_policy="in_meeting",
        user_facts=["fact A", "fact B"],
    )

    sep = "\n\n---\n\n"
    facts_block = "## Mémoire utilisateur\n- fact A\n- fact B"
    expected = sep.join(["SOUL", "ROLE", "USER", "CHANNEL", "POLICY", facts_block])
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
# Test 5 – user facts injected as last layer
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_user_facts_injected_last(tmp_path: Path) -> None:
    """User facts present — injected as final layer with correct header.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md", "SOUL")

    result = assemble_system_prompt(prompts, user_facts=["likes Python", "prefers French"])

    sep = "\n\n---\n\n"
    assert result == f"SOUL{sep}## Mémoire utilisateur\n- likes Python\n- prefers French"


# ---------------------------------------------------------------------------
# Test 6 – empty user_facts list not injected
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_user_facts_not_injected(tmp_path: Path) -> None:
    """user_facts=[] — no mémoire section added to output.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "soul" / "SOUL.md", "SOUL")

    result = assemble_system_prompt(prompts, user_facts=[])

    assert result == "SOUL"
    assert "Mémoire" not in result


# ---------------------------------------------------------------------------
# Test 7 – sender_id colon sanitized to underscore in filename
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sender_id_colon_sanitized(tmp_path: Path) -> None:
    """sender_id 'discord:123' resolves to users/discord_123.md (colon → underscore).

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)
    _write(prompts / "users" / "discord_123.md", "USER_PROFILE")

    result = assemble_system_prompt(prompts, sender_id="discord:123")

    assert result == "USER_PROFILE"


# ---------------------------------------------------------------------------
# Test 8 – no layers and no facts returns empty string
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_layers_returns_empty_string(tmp_path: Path) -> None:
    """No files, no facts — returns empty string without error.

    Args:
        tmp_path: pytest temporary directory.
    """
    prompts = _make_prompts_dir(tmp_path)

    result = assemble_system_prompt(prompts)

    assert result == ""


# ---------------------------------------------------------------------------
# Test 9 – empty file treated as missing
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
