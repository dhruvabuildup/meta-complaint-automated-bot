"""Unit tests for FastAPI endpoints and middleware."""

from unittest.mock import AsyncMock

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient

from meta_bot.api.app import create_app
from meta_bot.api.deps import get_db_session_dep, get_redis_dep
from meta_bot.config import Settings


@pytest.mark.asyncio
async def test_healthz_endpoint(test_settings: Settings) -> None:
    """Test healthz liveness endpoint returns 200 OK."""
    app = create_app(test_settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/healthz")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"status": "ok"}
        assert "X-Correlation-ID" in response.headers
        assert "X-Request-ID" in response.headers


@pytest.mark.asyncio
async def test_correlation_id_forwarding(test_settings: Settings) -> None:
    """Test that incoming correlation ID header is preserved."""
    app = create_app(test_settings)
    custom_id = "test-custom-trace-id-12345"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/healthz", headers={"X-Correlation-ID": custom_id})
        assert response.headers.get("X-Correlation-ID") == custom_id
        assert response.headers.get("X-Request-ID") == custom_id


@pytest.mark.asyncio
async def test_readyz_endpoint_healthy(test_settings: Settings) -> None:
    """Test readyz readiness endpoint when DB and Redis are healthy."""
    app = create_app(test_settings)

    mock_db = AsyncMock()
    mock_db.execute.return_value = None

    mock_redis = AsyncMock()
    mock_redis.ping.return_value = True

    app.dependency_overrides[get_db_session_dep] = lambda: mock_db
    app.dependency_overrides[get_redis_dep] = lambda: mock_redis

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/readyz")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "ready"
        assert data["database"] == "healthy"
        assert data["redis"] == "healthy"


@pytest.mark.asyncio
async def test_readyz_endpoint_unhealthy(test_settings: Settings) -> None:
    """Test readyz readiness endpoint returns 503 when Redis is unhealthy."""
    app = create_app(test_settings)

    mock_db = AsyncMock()
    mock_db.execute.return_value = None

    mock_redis = AsyncMock()
    mock_redis.ping.side_effect = ConnectionError("Redis unreachable")

    app.dependency_overrides[get_db_session_dep] = lambda: mock_db
    app.dependency_overrides[get_redis_dep] = lambda: mock_redis

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/readyz")
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        data = response.json()
        assert data["status"] == "not_ready"
        assert "unhealthy" in data["redis"]
