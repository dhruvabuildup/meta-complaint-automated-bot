"""Unit tests for administrative endpoints, authentication, and controls."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

from meta_bot.api.app import create_app
from meta_bot.api.deps import get_db_session_dep, get_redis_dep
from meta_bot.config import Settings
from meta_bot.domain.enums import ActionStatus, Platform, SendKind
from meta_bot.infra.db.models import ActionModel


@pytest.mark.asyncio
async def test_admin_auth_required(test_settings: Settings) -> None:
    """Admin routes reject requests without valid Bearer or X-Admin-Token headers."""
    app = create_app(test_settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. No token -> 401
        res = await client.get("/admin/status")
        assert res.status_code == 401

        # 2. Invalid token -> 401
        res = await client.get(
            "/admin/status", headers={"Authorization": "Bearer invalid_secret"}
        )
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_admin_kill_switch_toggle(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """POST /admin/kill-switch updates Redis state and records in audit log."""
    app = create_app(test_settings)

    mock_db = AsyncMock()
    app.dependency_overrides[get_redis_dep] = lambda: fake_redis
    app.dependency_overrides[get_db_session_dep] = lambda: mock_db

    admin_token = test_settings.ADMIN_TOKEN.get_secret_value()
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Turn ON
        res = await client.post(
            "/admin/kill-switch",
            json={"active": True, "reason": "emergency maintenance"},
            headers=headers,
        )
        assert res.status_code == 200
        assert res.json()["kill_switch_active"] is True
        assert await fake_redis.get("mb:v1:system:kill_switch") == "1"
        assert mock_db.execute.called

        # Turn OFF via X-Admin-Token header
        res2 = await client.post(
            "/admin/kill-switch",
            json={"active": False},
            headers={"X-Admin-Token": admin_token},
        )
        assert res2.status_code == 200
        assert res2.json()["kill_switch_active"] is False
        assert await fake_redis.get("mb:v1:system:kill_switch") == "0"


@pytest.mark.asyncio
async def test_admin_get_system_status(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """GET /admin/status returns accurate metric counters and queue depths."""
    app = create_app(test_settings)
    app.dependency_overrides[get_redis_dep] = lambda: fake_redis

    # Seed some dummy Redis counters
    await fake_redis.zadd("mb:v1:queue:sends", {"act_1": 1000.0, "act_2": 2000.0})
    await fake_redis.rpush("mb:v1:queue:events", "event_1")

    admin_token = test_settings.ADMIN_TOKEN.get_secret_value()
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.get("/admin/status", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["queue_depth_sends"] == 2
        assert data["queue_depth_events"] == 1
        assert data["queue_depth_dlq"] == 0
        assert data["hourly_cap"] == test_settings.MAX_PRIVATE_PER_HOUR
        assert data["daily_cap"] == test_settings.MAX_PRIVATE_PER_DAY
        assert data["circuit_breaker_state"] == "closed"


@pytest.mark.asyncio
async def test_admin_circuit_breaker_reset(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """POST /admin/circuit-breaker/reset clears error counts and closes the breaker."""
    app = create_app(test_settings)
    mock_db = AsyncMock()
    app.dependency_overrides[get_redis_dep] = lambda: fake_redis
    app.dependency_overrides[get_db_session_dep] = lambda: mock_db

    # Simulate open breaker and error counter
    await fake_redis.set("mb:v1:circuit_breaker:state", "open")
    await fake_redis.set("mb:v1:circuit_breaker:consecutive_errors", "5")

    admin_token = test_settings.ADMIN_TOKEN.get_secret_value()
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post(
            "/admin/circuit-breaker/reset", json={}, headers=headers
        )
        assert res.status_code == 200
        assert res.json()["circuit_breaker_state"] == "closed"
        assert await fake_redis.get("mb:v1:circuit_breaker:state") == "closed"
        assert await fake_redis.get("mb:v1:circuit_breaker:consecutive_errors") is None


@pytest.mark.asyncio
async def test_admin_list_and_manage_actions(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Test listing, retrying, and cancelling actions via admin endpoints."""
    app = create_app(test_settings)

    action_id = uuid4()
    mock_action = ActionModel(
        id=action_id,
        platform=Platform.INSTAGRAM.value,
        kind=SendKind.PRIVATE_REPLY.value,
        status=ActionStatus.FAILED.value,
        scheduled_at=datetime.now(timezone.utc),
        attempts=3,
        error_code="TRANSIENT_FAILURE",
        body_hash="hash",
    )

    mock_db = AsyncMock()
    # List actions returns our mock action
    mock_db.execute.return_value = MagicMock(
        scalars=lambda: MagicMock(all=lambda: [mock_action])
    )

    app.dependency_overrides[get_redis_dep] = lambda: fake_redis
    app.dependency_overrides[get_db_session_dep] = lambda: mock_db

    admin_token = test_settings.ADMIN_TOKEN.get_secret_value()
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. List actions
        res = await client.get("/admin/actions", headers=headers)
        assert res.status_code == 200
        items = res.json()
        assert len(items) == 1
        assert items[0]["id"] == str(action_id)

        # 2. Retry action
        mock_db.execute.return_value = MagicMock(scalar_one_or_none=lambda: mock_action)
        retry_res = await client.post(
            f"/admin/actions/{action_id}/retry", headers=headers
        )
        assert retry_res.status_code == 200
        assert mock_action.status == ActionStatus.QUEUED.value
        # Re-queued in Redis sorted set
        assert await fake_redis.zscore("mb:v1:queue:sends", str(action_id)) is not None

        # 3. Cancel action
        cancel_res = await client.post(
            f"/admin/actions/{action_id}/cancel", headers=headers
        )
        assert cancel_res.status_code == 200
        assert mock_action.status == ActionStatus.SKIPPED.value
        # Removed from Redis sorted set
        assert await fake_redis.zscore("mb:v1:queue:sends", str(action_id)) is None
