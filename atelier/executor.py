import asyncio
import logging

import httpx

from common.envelope import Envelope

logger = logging.getLogger("atelier.executor")

RETRIABLE = (httpx.ConnectError, httpx.TimeoutException)
RETRY_DELAYS = [2, 5, 15]  # seconds, progressive backoff


class ExhaustedRetriesError(Exception):
    """Raised when all retry attempts for a LiteLLM call have been exhausted."""


async def execute_with_resilience(
    http_client: httpx.AsyncClient,
    envelope: Envelope,
    context: list[dict],
    litellm_url: str,
    model: str,
    api_key: str = "",
) -> str:
    """Call LiteLLM with retry logic on transient errors.

    Retries up to 3 times with progressive backoff on ConnectError,
    TimeoutException, and HTTP 502/503/504 responses (e.g. LiteLLM restarting).
    Non-retriable errors (400, 401, etc.) are re-raised immediately.

    Args:
        http_client: Shared httpx async client.
        envelope: The task envelope being processed (used for logging).
        context: List of role/content message dicts for the LLM prompt.
        litellm_url: Base URL of the LiteLLM proxy (without path).
        model: LiteLLM model identifier to use for the request.
        api_key: Bearer token sent in the Authorization header. When empty,
            no Authorization header is sent (development / unauthenticated proxy).

    Returns:
        The assistant reply text extracted from the LiteLLM response.

    Raises:
        ExhaustedRetriesError: All retry attempts failed due to transient errors.
        httpx.HTTPStatusError: Non-retriable HTTP error (4xx except 502/503/504).
        Exception: Any other unexpected error from the HTTP client.
    """
    last_exception: Exception | None = None

    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        try:
            logger.warning(
                f"[{envelope.correlation_id}] LiteLLM call attempt {attempt}/{len(RETRY_DELAYS)}"
            )
            headers: dict[str, str] = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            response = await http_client.post(
                f"{litellm_url}/chat/completions",
                json={
                    "model": model,
                    "messages": context,
                },
                headers=headers,
                timeout=60.0,
            )
            response.raise_for_status()
            reply: str = response.json()["choices"][0]["message"]["content"]
            logger.warning(
                f"[{envelope.correlation_id}] LiteLLM call succeeded on attempt {attempt}"
            )
            return reply

        except RETRIABLE as exc:
            last_exception = exc
            logger.warning(
                f"[{envelope.correlation_id}] LiteLLM unreachable "
                f"(attempt {attempt}/{len(RETRY_DELAYS)}): {exc}"
            )
            if attempt < len(RETRY_DELAYS):
                await asyncio.sleep(delay)
            else:
                raise ExhaustedRetriesError(
                    f"LiteLLM down after {attempt} retries: {exc}"
                ) from exc

        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (502, 503, 504):
                last_exception = exc
                logger.warning(
                    f"[{envelope.correlation_id}] LiteLLM returned HTTP "
                    f"{exc.response.status_code} (attempt {attempt}/{len(RETRY_DELAYS)}): {exc}"
                )
                if attempt < len(RETRY_DELAYS):
                    await asyncio.sleep(delay)
                    continue
                raise ExhaustedRetriesError(
                    f"LiteLLM returned {exc.response.status_code} after {attempt} retries"
                ) from exc
            # Non-retriable HTTP error — raise immediately
            logger.error(
                f"[{envelope.correlation_id}] Non-retriable HTTP error "
                f"{exc.response.status_code}: {exc}"
            )
            raise

    # Should not be reached, but satisfy the type checker
    raise ExhaustedRetriesError(
        f"LiteLLM exhausted all retries for {envelope.correlation_id}"
    ) from last_exception
