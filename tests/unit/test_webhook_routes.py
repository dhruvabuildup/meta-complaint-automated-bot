"""Unit tests for Meta webhook GET handshake and POST ingestion routes."""

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient

from meta_bot.api.app import create_app
from meta_bot.api.deps import get_db_session_dep, get_redis_dep
from meta_bot.config import Settings
from meta_bot.infra.redis.keys import event_queue_key


def _compute_sig(body: bytes, secret: str) -> str:
    """Helper to compute valid sha256 header."""
    mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


@pytest.mark.asyncio
async def test_webhook_handshake_success(test_settings: Settings) -> None:
    """GET /webhook returns 200 and challenge string when verify token matches."""
    app = create_app(test_settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": test_settings.VERIFY_TOKEN.get_secret_value(),
                "hub.challenge": "meta_handshake_challenge_string_12345",
            },
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.text == "meta_handshake_challenge_string_12345"
        assert "text/plain" in response.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_webhook_handshake_invalid_token(test_settings: Settings) -> None:
    """GET /webhook returns 403 when verify token does not match."""
    app = create_app(test_settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong_unauthorized_token",
                "hub.challenge": "challenge_12345",
            },
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_webhook_handshake_invalid_mode(test_settings: Settings) -> None:
    """GET /webhook returns 403 when hub.mode is not subscribe."""
    app = create_app(test_settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/webhook",
            params={
                "hub.mode": "unsubscribe",
                "hub.verify_token": test_settings.VERIFY_TOKEN.get_secret_value(),
                "hub.challenge": "challenge_12345",
            },
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_webhook_post_missing_signature(test_settings: Settings) -> None:
    """POST /webhook returns 403 if X-Hub-Signature-256 header is absent."""
    app = create_app(test_settings)
    mock_db = AsyncMock()
    mock_redis = AsyncMock()
    app.dependency_overrides[get_db_session_dep] = lambda: mock_db
    app.dependency_overrides[get_redis_dep] = lambda: mock_redis

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/webhook",
            content=b'{"object": "instagram"}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        mock_db.add.assert_not_called()
        mock_redis.rpush.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_post_invalid_signature(test_settings: Settings) -> None:
    """POST /webhook returns 403 if signature is wrong or tampered."""
    app = create_app(test_settings)
    mock_db = AsyncMock()
    mock_redis = AsyncMock()
    app.dependency_overrides[get_db_session_dep] = lambda: mock_db
    app.dependency_overrides[get_redis_dep] = lambda: mock_redis

    body = b'{"object": "instagram", "entry": []}'
    headers = {
        "X-Hub-Signature-256": "sha256=0000000000000000000000000000000000000000000000000000000000000000",
        "Content-Type": "application/json",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/webhook", content=body, headers=headers)
        assert response.status_code == status.HTTP_403_FORBIDDEN
        mock_db.add.assert_not_called()
        mock_redis.rpush.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_post_valid_ig_comment(test_settings: Settings) -> None:
    """POST /webhook with valid signature stores raw event and enqueues to Redis."""
    app = create_app(test_settings)
    mock_db = AsyncMock()
    mock_redis = AsyncMock()
    app.dependency_overrides[get_db_session_dep] = lambda: mock_db
    app.dependency_overrides[get_redis_dep] = lambda: mock_redis

    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "17841400000000001",
                "time": 1712000000,
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "id": "17900000000000001",
                            "text": "How much does this cost?",
                            "from": {
                                "id": "17841411111111111",
                                "username": "customer_jane",
                            },
                            "media": {"id": "17999999999999999"},
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    sig = _compute_sig(body, test_settings.APP_SECRET.get_secret_value())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/webhook",
            content=body,
            headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"status": "ok"}

        # Verify DB raw event was recorded
        mock_db.execute.assert_called_once()

        # Verify Redis rpush was called with serialized item
        mock_redis.rpush.assert_called_once()
        call_args = mock_redis.rpush.call_args[0]
        assert call_args[0] == event_queue_key()
        enqueued_data = json.loads(call_args[1])
        assert enqueued_data["object"] == "instagram"
        assert enqueued_data["field"] == "comments"
        assert enqueued_data["value"]["id"] == "17900000000000001"


@pytest.mark.asyncio
async def test_webhook_post_malformed_json_signed(test_settings: Settings) -> None:
    """POST /webhook with valid signature but malformed JSON returns 200 and records review."""
    app = create_app(test_settings)
    mock_db = AsyncMock()
    mock_redis = AsyncMock()
    app.dependency_overrides[get_db_session_dep] = lambda: mock_db
    app.dependency_overrides[get_redis_dep] = lambda: mock_redis

    malformed_body = b'{"object": "instagram", "entry": [INVALID_JSON_BODY'
    sig = _compute_sig(malformed_body, test_settings.APP_SECRET.get_secret_value())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/webhook",
            content=malformed_body,
            headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
        )
        # Must return 200 to prevent Meta retry storm for bad payloads
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"status": "malformed_recorded"}

        # Recorded in raw_repo for review
        mock_db.execute.assert_called_once()
        # Not enqueued to worker queue
        mock_redis.rpush.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_latency_benchmark(test_settings: Settings) -> None:
    """Benchmark POST /webhook response latency; p95 must be well under 200ms."""
    app = create_app(test_settings)
    mock_db = AsyncMock()
    mock_redis = AsyncMock()
    app.dependency_overrides[get_db_session_dep] = lambda: mock_db
    app.dependency_overrides[get_redis_dep] = lambda: mock_redis

    payload = {"object": "page", "entry": []}
    body = json.dumps(payload).encode("utf-8")
    sig = _compute_sig(body, test_settings.APP_SECRET.get_secret_value())
    headers = {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}

    durations: list[float] = []
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for _ in range(25):
            t0 = time.perf_counter()
            resp = await client.post("/webhook", content=body, headers=headers)
            t1 = time.perf_counter()
            assert resp.status_code == status.HTTP_200_OK
            durations.append(t1 - t0)

    durations.sort()
    # 95th percentile index
    p95_idx = int(len(durations) * 0.95)
    p95_sec = durations[p95_idx]

    # Meta requirement: target under 200ms
    assert p95_sec < 0.200, f"p95 latency was {p95_sec:.4f}s, expected < 0.200s"
