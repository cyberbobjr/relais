"""Standard health check for RELAIS bricks.

Provides a single async function that returns a uniform health
dict, suitable for HTTP endpoints, supervisord probes, or the
Le Vigile admin brick.
"""
import logging
import time
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# Module-load time used to compute uptime
_START_TIME: float = time.monotonic()


async def health(
    brick_name: str,
    redis: Optional[aioredis.Redis] = None,
) -> dict:
    """Returns a standard health check dictionary for a RELAIS brick.

    Checks Redis connectivity via PING when a connection is supplied.
    Uptime is measured from the moment this module was first imported.

    Args:
        brick_name: Human-readable name of the brick (e.g. ``"atelier"``).
        redis: Optional active async Redis connection. When provided, a PING
               is issued to verify connectivity. If ``None``, the ``"redis"``
               field is reported as ``"n/a"``.

    Returns:
        A dictionary with the following keys:

        - ``status`` (``"ok"`` | ``"degraded"``): Overall health.
          Degrades when Redis is provided but unreachable.
        - ``brick`` (str): The brick name passed in.
        - ``uptime_seconds`` (float): Seconds since module import,
          rounded to two decimal places.
        - ``redis`` (``"ok"`` | ``"error"`` | ``"n/a"``): Redis status.

    Example::

        result = await health("atelier", redis_conn)
        # {"status": "ok", "brick": "atelier", "uptime_seconds": 42.1, "redis": "ok"}
    """
    uptime = round(time.monotonic() - _START_TIME, 2)

    redis_status: str
    if redis is None:
        redis_status = "n/a"
    else:
        try:
            await redis.ping()
            redis_status = "ok"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Health check Redis PING failed for brick '%s': %s", brick_name, exc)
            redis_status = "error"

    overall_status = "ok" if redis_status in ("ok", "n/a") else "degraded"

    return {
        "status": overall_status,
        "brick": brick_name,
        "uptime_seconds": uptime,
        "redis": redis_status,
    }
