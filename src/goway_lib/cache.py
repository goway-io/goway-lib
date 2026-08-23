import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)


async def cache_aside(
    redis: Redis,
    key: str,
    loader: Callable[[], Awaitable[Any]],
    *,
    ttl: int = 300,
) -> Any:
    try:
        cached = await redis.get(key)
        if cached is not None:
            return json.loads(cached)
    except Exception as exc:
        logger.warning("cache get failed key=%s: %s", key, exc)

    value = await loader()

    try:
        await redis.setex(key, ttl, _dumps(value))
    except Exception as exc:
        logger.warning("cache set failed key=%s: %s", key, exc)
    return value


async def cache_invalidate(redis: Redis, *keys: str) -> None:
    if not keys:
        return
    try:
        await redis.delete(*keys)
    except Exception as exc:
        logger.warning("cache invalidate failed keys=%s: %s", keys, exc)
