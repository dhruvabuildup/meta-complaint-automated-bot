"""Unit tests for Meta GraphClient retry logic, error handling, and requests."""

import httpx
import pytest
import respx

from meta_bot.adapters.meta.graph_client import GraphClient
from meta_bot.config import Settings
from meta_bot.errors import MetaAuthError, MetaPermanentError


@pytest.mark.asyncio
async def test_retry_on_500_then_success(test_settings: Settings) -> None:
    """GraphClient must retry on 500 transient errors and return successful response."""
    async with httpx.AsyncClient() as http_client:
        client = GraphClient(settings=test_settings, http_client=http_client)
        url = f"{test_settings.GRAPH_BASE_URL}/{test_settings.GRAPH_VERSION}/me"

        with respx.mock(assert_all_called=True) as respx_mock:
            # First attempt: 500 Internal Server Error
            route1 = respx_mock.get(url).respond(
                status_code=500,
                json={"error": {"code": 1, "message": "Internal error"}},
            )
            # Second attempt: 200 OK
            route2 = respx_mock.get(url).respond(
                status_code=200,
                json={"id": "12345", "name": "Test Page"},
            )

            result = await client.get("/me")
            assert result == {"id": "12345", "name": "Test Page"}
            assert route1.call_count == 1
            assert route2.call_count == 1


@pytest.mark.asyncio
async def test_no_retry_on_400_permanent_error(test_settings: Settings) -> None:
    """GraphClient must NOT retry on permanent client errors (400, 401, 403)."""
    async with httpx.AsyncClient() as http_client:
        client = GraphClient(settings=test_settings, http_client=http_client)
        url = f"{test_settings.GRAPH_BASE_URL}/{test_settings.GRAPH_VERSION}/me"

        with respx.mock(assert_all_called=True) as respx_mock:
            route = respx_mock.get(url).respond(
                status_code=400,
                json={"error": {"code": 100, "message": "Invalid parameter"}},
            )

            with pytest.raises(MetaPermanentError):
                await client.get("/me")

            # Must not retry: call count exactly 1
            assert route.call_count == 1


@pytest.mark.asyncio
async def test_no_retry_on_auth_error(test_settings: Settings) -> None:
    """GraphClient must NOT retry on expired or invalid token errors."""
    async with httpx.AsyncClient() as http_client:
        client = GraphClient(settings=test_settings, http_client=http_client)
        url = f"{test_settings.GRAPH_BASE_URL}/{test_settings.GRAPH_VERSION}/me"

        with respx.mock(assert_all_called=True) as respx_mock:
            route = respx_mock.get(url).respond(
                status_code=400,
                json={"error": {"code": 190, "message": "Invalid token"}},
            )

            with pytest.raises(MetaAuthError):
                await client.get("/me")

            assert route.call_count == 1


@pytest.mark.asyncio
async def test_retry_on_429_rate_limit(test_settings: Settings) -> None:
    """GraphClient must retry on rate limit (429) errors."""
    async with httpx.AsyncClient() as http_client:
        client = GraphClient(settings=test_settings, http_client=http_client)
        url = f"{test_settings.GRAPH_BASE_URL}/{test_settings.GRAPH_VERSION}/me"

        with respx.mock(assert_all_called=True) as respx_mock:
            route1 = respx_mock.get(url).respond(
                status_code=429,
                json={"error": {"code": 613, "message": "Rate limit reached"}},
            )
            route2 = respx_mock.get(url).respond(
                status_code=200,
                json={"id": "12345"},
            )

            result = await client.get("/me")
            assert result == {"id": "12345"}
            assert route1.call_count == 1
            assert route2.call_count == 1


@pytest.mark.asyncio
async def test_debug_token_call(test_settings: Settings) -> None:
    """Test debug_token endpoint and app token construction."""
    async with httpx.AsyncClient() as http_client:
        client = GraphClient(settings=test_settings, http_client=http_client)
        url = (
            f"{test_settings.GRAPH_BASE_URL}/{test_settings.GRAPH_VERSION}/debug_token"
        )

        with respx.mock(assert_all_called=True) as respx_mock:
            respx_mock.get(url).respond(
                status_code=200,
                json={"data": {"is_valid": True, "app_id": test_settings.APP_ID}},
            )

            res = await client.debug_token("user_token_123")
            assert res["data"]["is_valid"] is True


@pytest.mark.asyncio
async def test_get_page_instagram_account(test_settings: Settings) -> None:
    """Test discovering linked Instagram professional account from Page."""
    async with httpx.AsyncClient() as http_client:
        client = GraphClient(settings=test_settings, http_client=http_client)
        page_id = "page_98765"
        url = f"{test_settings.GRAPH_BASE_URL}/{test_settings.GRAPH_VERSION}/{page_id}"

        with respx.mock(assert_all_called=True) as respx_mock:
            respx_mock.get(url).respond(
                status_code=200,
                json={"instagram_business_account": {"id": "ig_54321"}},
            )

            res = await client.get_page_instagram_account(page_id)
            assert res["instagram_business_account"]["id"] == "ig_54321"


@pytest.mark.asyncio
async def test_demo_mode_skips_post_mutation(test_settings: Settings) -> None:
    """When DEMO_MODE is true, write operations must not call external network."""
    demo_settings = test_settings.model_copy(update={"DEMO_MODE": True})
    async with httpx.AsyncClient() as http_client:
        client = GraphClient(settings=demo_settings, http_client=http_client)
        res = await client.post("/some_endpoint", json={"message": "hello"})
        assert res.get("demo_mode") is True
