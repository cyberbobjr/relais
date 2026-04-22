"""Unit tests for atelier.transient_errors — TDD RED first.

Tests validate:
- Constants are importable from atelier.transient_errors
- _is_transient_provider_error correctly classifies known provider errors by name
- _is_transient_provider_error correctly classifies ValueErrors with rate-limit messages
- _is_transient_provider_error returns False for unknown/permanent errors
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helper fake error classes (matched by name, not by inheritance)
# ---------------------------------------------------------------------------


class RateLimitError(Exception):
    pass


class InternalServerError(Exception):
    pass


class APIConnectionError(Exception):
    pass


class APITimeoutError(Exception):
    pass


class ServiceUnavailableError(Exception):
    pass


class UnknownProviderError(Exception):
    pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_transient_errors_importable() -> None:
    """_is_transient_provider_error and constants must be importable."""
    from atelier.transient_errors import (  # noqa: F401
        _TRANSIENT_ERROR_NAMES,
        _TRANSIENT_VALUE_ERROR_PATTERNS,
        _is_transient_provider_error,
    )


@pytest.mark.unit
def test_transient_error_names_is_frozenset() -> None:
    """_TRANSIENT_ERROR_NAMES must be a frozenset."""
    from atelier.transient_errors import _TRANSIENT_ERROR_NAMES

    assert isinstance(_TRANSIENT_ERROR_NAMES, frozenset)


@pytest.mark.unit
def test_transient_error_names_contains_expected_names() -> None:
    """_TRANSIENT_ERROR_NAMES must contain the canonical provider error names."""
    from atelier.transient_errors import _TRANSIENT_ERROR_NAMES

    expected = {
        "RateLimitError",
        "InternalServerError",
        "APIConnectionError",
        "APITimeoutError",
        "ServiceUnavailableError",
    }
    assert expected.issubset(_TRANSIENT_ERROR_NAMES)


# ---------------------------------------------------------------------------
# _is_transient_provider_error — class name matching
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rate_limit_error_is_transient() -> None:
    """RateLimitError (matched by class name) must be classified as transient."""
    from atelier.transient_errors import _is_transient_provider_error

    assert _is_transient_provider_error(RateLimitError("rate limited"))


@pytest.mark.unit
def test_internal_server_error_is_transient() -> None:
    """InternalServerError (matched by class name) must be classified as transient."""
    from atelier.transient_errors import _is_transient_provider_error

    assert _is_transient_provider_error(InternalServerError("500"))


@pytest.mark.unit
def test_api_connection_error_is_transient() -> None:
    """APIConnectionError (matched by class name) must be classified as transient."""
    from atelier.transient_errors import _is_transient_provider_error

    assert _is_transient_provider_error(APIConnectionError("timeout"))


@pytest.mark.unit
def test_api_timeout_error_is_transient() -> None:
    """APITimeoutError (matched by class name) must be classified as transient."""
    from atelier.transient_errors import _is_transient_provider_error

    assert _is_transient_provider_error(APITimeoutError("timed out"))


@pytest.mark.unit
def test_service_unavailable_error_is_transient() -> None:
    """ServiceUnavailableError (matched by class name) must be classified as transient."""
    from atelier.transient_errors import _is_transient_provider_error

    assert _is_transient_provider_error(ServiceUnavailableError("503"))


@pytest.mark.unit
def test_unknown_error_is_not_transient() -> None:
    """Unknown error class must not be classified as transient."""
    from atelier.transient_errors import _is_transient_provider_error

    assert not _is_transient_provider_error(UnknownProviderError("bug"))


@pytest.mark.unit
def test_plain_runtime_error_is_not_transient() -> None:
    """RuntimeError must not be classified as transient."""
    from atelier.transient_errors import _is_transient_provider_error

    assert not _is_transient_provider_error(RuntimeError("unexpected"))


# ---------------------------------------------------------------------------
# _is_transient_provider_error — ValueError substring matching
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_value_error_rate_limit_is_transient() -> None:
    """ValueError containing 'rate limit' must be classified as transient."""
    from atelier.transient_errors import _is_transient_provider_error

    assert _is_transient_provider_error(ValueError("rate limit exceeded"))


@pytest.mark.unit
def test_value_error_upstream_error_is_transient() -> None:
    """ValueError containing 'upstream error' must be classified as transient."""
    from atelier.transient_errors import _is_transient_provider_error

    assert _is_transient_provider_error(ValueError("upstream error: 502 Bad Gateway"))


@pytest.mark.unit
def test_value_error_overloaded_is_transient() -> None:
    """ValueError containing 'overloaded' must be classified as transient."""
    from atelier.transient_errors import _is_transient_provider_error

    assert _is_transient_provider_error(ValueError("model overloaded, try again"))


@pytest.mark.unit
def test_value_error_too_many_requests_is_transient() -> None:
    """ValueError containing 'too many requests' must be classified as transient."""
    from atelier.transient_errors import _is_transient_provider_error

    assert _is_transient_provider_error(ValueError("too many requests"))


@pytest.mark.unit
def test_value_error_code_502_is_transient() -> None:
    """ValueError containing 'code: 502' must be classified as transient."""
    from atelier.transient_errors import _is_transient_provider_error

    assert _is_transient_provider_error(ValueError("code: 502"))


@pytest.mark.unit
def test_value_error_code_503_is_transient() -> None:
    """ValueError containing 'code: 503' must be classified as transient."""
    from atelier.transient_errors import _is_transient_provider_error

    assert _is_transient_provider_error(ValueError("code: 503"))


@pytest.mark.unit
def test_value_error_unknown_schema_is_not_transient() -> None:
    """ValueError with an irrelevant message must not be classified as transient."""
    from atelier.transient_errors import _is_transient_provider_error

    assert not _is_transient_provider_error(ValueError("unexpected schema mismatch"))


@pytest.mark.unit
def test_value_error_empty_message_is_not_transient() -> None:
    """ValueError with empty message must not be classified as transient."""
    from atelier.transient_errors import _is_transient_provider_error

    assert not _is_transient_provider_error(ValueError(""))
