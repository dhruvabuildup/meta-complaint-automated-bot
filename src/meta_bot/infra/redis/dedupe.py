"""Redis implementation of DedupePort."""

from redis.asyncio import Redis

from meta_bot.services.ports import DedupePort


class RedisDedupeAdapter(DedupePort):
    """Atomic deduplication adapter backed by Redis SET NX EX."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def set_nx(self, key: str, value: str, ttl_seconds: int = 604800) -> bool:
        """Atomically set key if not exists with TTL (default 7 days).

        Meta rule: Dedupe comments and messages over a 7-day window.
        Returns True if newly set, False if key already exists.
        """
        res = await self._redis.set(name=key, value=value, nx=True, ex=ttl_seconds)
        return bool(res)
