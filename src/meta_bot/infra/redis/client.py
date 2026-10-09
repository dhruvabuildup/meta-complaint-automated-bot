"""Redis client factory and kill switch operations."""

from redis.asyncio import Redis, from_url

from meta_bot.infra.redis.keys import kill_switch_key


def get_redis_client(redis_url: str) -> Redis:
    """Create asynchronous Redis client connection."""
    return from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=True,
    )


async def is_killed(redis_client: Redis) -> bool:
    """Check if global automation kill switch is currently engaged.

    When killed, all outbound sends and LLM generations must cease immediately.
    """
    val = await redis_client.get(kill_switch_key())
    if val is None:
        return False
    return str(val).strip().lower() in ("1", "true", "yes")


async def set_killed(redis_client: Redis, killed: bool) -> None:
    """Engage or release the global automation kill switch."""
    val = "1" if killed else "0"
    await redis_client.set(kill_switch_key(), val)
