"""Unit tests for Redis operations and kill switch helpers."""

import fakeredis.aioredis
import pytest

from meta_bot.infra.redis.client import is_killed, set_killed


@pytest.mark.asyncio
async def test_kill_switch_lifecycle(fake_redis: fakeredis.aioredis.FakeRedis) -> None:
    """Test engaging and releasing the kill switch."""
    # Initially not killed
    assert await is_killed(fake_redis) is False

    # Engage kill switch
    await set_killed(fake_redis, True)
    assert await is_killed(fake_redis) is True

    # Release kill switch
    await set_killed(fake_redis, False)
    assert await is_killed(fake_redis) is False
