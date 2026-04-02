"""Tests TDD — common/command_utils.py (Phase 1 RED).

Covers:
- is_command(content) → bool
- extract_command_name(content) → str | None
- KNOWN_COMMANDS: frozenset[str]
"""
import pytest

from common import command_utils


# ---------------------------------------------------------------------------
# is_command
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestIsCommand:
    """is_command() returns True iff content begins with '/' after stripping."""

    def test_slash_command_returns_true(self) -> None:
        """'/clear' → True."""
        assert command_utils.is_command("/clear") is True

    def test_slash_command_with_args_returns_true(self) -> None:
        """/clear all → True."""
        assert command_utils.is_command("/clear all") is True

    def test_slash_only_returns_false(self) -> None:
        """'/' alone (no following character) → False."""
        assert command_utils.is_command("/") is False

    def test_empty_string_returns_false(self) -> None:
        """Empty string → False."""
        assert command_utils.is_command("") is False

    def test_plain_message_returns_false(self) -> None:
        """Regular message without slash → False."""
        assert command_utils.is_command("bonjour le monde") is False

    def test_leading_whitespace_stripped(self) -> None:
        """Leading whitespace before '/' is stripped before check."""
        assert command_utils.is_command("  /help") is True

    def test_trailing_whitespace_stripped(self) -> None:
        """Trailing whitespace after command → still True."""
        assert command_utils.is_command("/help   ") is True

    def test_double_quoted_slash_command_returns_true(self) -> None:
        """Double-quoted command '"/help"' → True (Discord workaround)."""
        assert command_utils.is_command('"/help"') is True

    def test_single_quoted_slash_command_returns_true(self) -> None:
        """Single-quoted command \"'/help'\" → True."""
        assert command_utils.is_command("'/help'") is True

    def test_non_slash_start_returns_false(self) -> None:
        """Message starting with non-slash character → False."""
        assert command_utils.is_command("hello /clear") is False

    def test_none_is_not_accepted(self) -> None:
        """Passing None should raise TypeError — function expects str."""
        with pytest.raises((TypeError, AttributeError)):
            command_utils.is_command(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# extract_command_name
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestExtractCommandName:
    """extract_command_name() returns the bare command name (no slash, no args)."""

    def test_simple_command_returns_name(self) -> None:
        """/clear → 'clear'."""
        assert command_utils.extract_command_name("/clear") == "clear"

    def test_command_with_args_returns_name_only(self) -> None:
        """/clear all extra → 'clear' (args discarded)."""
        assert command_utils.extract_command_name("/clear all extra") == "clear"

    def test_command_with_leading_whitespace(self) -> None:
        """'  /clear' → 'clear'."""
        assert command_utils.extract_command_name("  /clear") == "clear"

    def test_plain_message_returns_none(self) -> None:
        """Non-command returns None."""
        assert command_utils.extract_command_name("bonjour") is None

    def test_slash_only_returns_none(self) -> None:
        """'/' alone → None."""
        assert command_utils.extract_command_name("/") is None

    def test_empty_string_returns_none(self) -> None:
        """Empty string → None."""
        assert command_utils.extract_command_name("") is None

    def test_double_quoted_command(self) -> None:
        """'"/help"' → 'help'."""
        assert command_utils.extract_command_name('"/help"') == "help"

    def test_single_quoted_command(self) -> None:
        """\"'/help'\" → 'help'."""
        assert command_utils.extract_command_name("'/help'") == "help"

    def test_command_name_is_lowercased(self) -> None:
        """/CLEAR → 'clear' (lowercase normalisation)."""
        assert command_utils.extract_command_name("/CLEAR") == "clear"

    def test_help_command(self) -> None:
        """/help → 'help'."""
        assert command_utils.extract_command_name("/help") == "help"

    def test_unknown_slash_command_returns_name(self) -> None:
        """/xyz (unknown but syntactically valid) → 'xyz'."""
        assert command_utils.extract_command_name("/xyz") == "xyz"


# ---------------------------------------------------------------------------
# KNOWN_COMMANDS
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestKnownCommands:
    """KNOWN_COMMANDS is a frozenset derived from commandant's COMMAND_REGISTRY."""

    def test_known_commands_is_frozenset(self) -> None:
        """KNOWN_COMMANDS must be a frozenset."""
        assert isinstance(command_utils.KNOWN_COMMANDS, frozenset)

    def test_known_commands_contains_clear(self) -> None:
        """'clear' is a known command."""
        assert "clear" in command_utils.KNOWN_COMMANDS

    def test_known_commands_contains_help(self) -> None:
        """'help' is a known command."""
        assert "help" in command_utils.KNOWN_COMMANDS

    def test_known_commands_all_lowercase(self) -> None:
        """All entries in KNOWN_COMMANDS are lowercase strings."""
        for cmd in command_utils.KNOWN_COMMANDS:
            assert cmd == cmd.lower(), f"Command {cmd!r} is not lowercase"

    def test_known_commands_non_empty(self) -> None:
        """KNOWN_COMMANDS must not be empty."""
        assert len(command_utils.KNOWN_COMMANDS) > 0

    def test_known_commands_consistent_with_commandant_registry(self) -> None:
        """KNOWN_COMMANDS must match commandant.commands.KNOWN_COMMANDS."""
        from commandant.commands import KNOWN_COMMANDS as commandant_known
        assert command_utils.KNOWN_COMMANDS == commandant_known
