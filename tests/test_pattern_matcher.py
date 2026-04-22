"""Unit tests for common.pattern_matcher — parse_patterns and matches.

Tests are RED-first (written before the implementation exists).
"""

import pytest

from common.pattern_matcher import matches, parse_patterns


# ---------------------------------------------------------------------------
# parse_patterns
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_patterns_none_returns_empty_tuple() -> None:
    """None input must normalise to an empty tuple."""
    assert parse_patterns(None) == ()


@pytest.mark.unit
def test_parse_patterns_empty_list_returns_empty_tuple() -> None:
    """An empty list must normalise to an empty tuple."""
    assert parse_patterns([]) == ()


@pytest.mark.unit
def test_parse_patterns_bare_string_returns_single_element_tuple() -> None:
    """A plain string must be wrapped into a one-element tuple."""
    assert parse_patterns("*") == ("*",)


@pytest.mark.unit
def test_parse_patterns_list_of_strings() -> None:
    """A list of strings must be converted to a tuple preserving order."""
    assert parse_patterns(["foo", "bar-*"]) == ("foo", "bar-*")


@pytest.mark.unit
def test_parse_patterns_returns_tuple_type() -> None:
    """Return value must always be a tuple, not a list."""
    result = parse_patterns(["a", "b"])
    assert isinstance(result, tuple)


@pytest.mark.unit
def test_parse_patterns_empty_tuple_input_returns_empty_tuple() -> None:
    """An empty tuple must normalise to an empty tuple."""
    assert parse_patterns(()) == ()


@pytest.mark.unit
def test_parse_patterns_tuple_of_strings() -> None:
    """A tuple of strings must be converted to a tuple preserving order."""
    assert parse_patterns(("foo", "bar-*")) == ("foo", "bar-*")


# ---------------------------------------------------------------------------
# matches
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_matches_exact_match_returns_true() -> None:
    """Exact name match against a single-element pattern tuple."""
    assert matches("foo", ("foo",)) is True


@pytest.mark.unit
def test_matches_no_match_returns_false() -> None:
    """Name that does not match any pattern returns False."""
    assert matches("foo", ("bar",)) is False


@pytest.mark.unit
def test_matches_glob_pattern_returns_true() -> None:
    """Wildcard glob pattern must match correctly."""
    assert matches("bar-agent", ("bar-*",)) is True


@pytest.mark.unit
def test_matches_empty_patterns_returns_false() -> None:
    """Empty patterns tuple must return False (fail-closed: nothing authorised)."""
    assert matches("anything", ()) is False


@pytest.mark.unit
def test_matches_star_pattern_returns_true() -> None:
    """A '*' pattern must match any name."""
    assert matches("anything", ("*",)) is True


@pytest.mark.unit
def test_matches_first_pattern_matches() -> None:
    """Returns True when only the first pattern matches."""
    assert matches("alpha", ("alpha", "beta")) is True


@pytest.mark.unit
def test_matches_second_pattern_matches() -> None:
    """Returns True when only the second pattern matches."""
    assert matches("beta", ("alpha", "beta")) is True


@pytest.mark.unit
def test_matches_no_pattern_matches_multiple() -> None:
    """Returns False when the name matches none of the patterns."""
    assert matches("gamma", ("alpha", "beta")) is False
