"""Transient error detection for the Atelier agent executor.

Provides the ``_is_transient_provider_error`` function and its supporting
constants, extracted from ``atelier/agent_executor.py`` to keep that module
under the 800-line limit.

Re-exported in ``atelier/agent_executor.py`` for backward compatibility.
"""

from __future__ import annotations


# Error class names raised by LLM providers (anthropic, openai, google, …) that
# indicate a transient condition — caller must NOT ACK the message so it stays in
# the PEL for automatic re-delivery.  We detect by name to stay provider-agnostic
# and avoid importing provider SDKs directly.
_TRANSIENT_ERROR_NAMES: frozenset[str] = frozenset(
    {
        "RateLimitError",
        "InternalServerError",
        "APIConnectionError",
        "APITimeoutError",
        "ServiceUnavailableError",
    }
)

# Substrings matched (case-insensitive) inside a ValueError message to detect
# upstream rate-limit or throttle errors forwarded as plain ValueError by
# proxy layers (e.g. OpenRouter wrapping Alibaba / other upstream providers).
_TRANSIENT_VALUE_ERROR_PATTERNS: tuple[str, ...] = (
    "rate limit",
    "rate increased too quickly",
    "too many requests",
    "upstream error",
    "code: 502",
    "code: 503",
    "overloaded",
)


def _is_transient_provider_error(exc: BaseException) -> bool:
    """Return True if *exc* is a known transient provider error.

    Two detection strategies:
    - Class name match against ``_TRANSIENT_ERROR_NAMES`` (provider SDK errors).
    - Substring match on ``ValueError`` messages for proxy layers (e.g.
      OpenRouter forwarding upstream rate-limit / 502 errors as plain
      ``ValueError`` rather than a typed SDK exception).

    Args:
        exc: The exception to classify.

    Returns:
        True if the error is transient and the caller should not ACK the message.
    """
    if type(exc).__name__ in _TRANSIENT_ERROR_NAMES:
        return True
    if isinstance(exc, ValueError):
        msg = str(exc).lower()
        return any(pattern in msg for pattern in _TRANSIENT_VALUE_ERROR_PATTERNS)
    return False
