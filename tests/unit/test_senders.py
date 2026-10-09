"""Unit tests for MetaMessageSender adapter with Graph API error mapping and demo mode."""

import httpx
import pytest
import respx

from meta_bot.adapters.meta.graph_client import GraphClient
from meta_bot.adapters.meta.senders import MetaMessageSender
from meta_bot.config import Settings
from meta_bot.domain.enums import ActionStatus, Platform
from meta_bot.errors import MetaAuthError, MetaWindowError


@pytest.mark.asyncio
async def test_public_reply_instagram(test_settings: Settings) -> None:
    """Instagram public reply posts to /{ig-comment-id}/replies."""
    comment_id = "1790011223344"
    url = f"{test_settings.GRAPH_BASE_URL}/{test_settings.GRAPH_VERSION}/{comment_id}/replies"

    async with httpx.AsyncClient() as http_client:
        graph = GraphClient(settings=test_settings, http_client=http_client)
        sender = MetaMessageSender(graph_client=graph, settings=test_settings)

        with respx.mock(assert_all_called=True) as respx_mock:
            respx_mock.post(url).respond(status_code=200, json={"id": "reply_9988"})

            result = await sender.public_reply(
                platform=Platform.INSTAGRAM,
                comment_id=comment_id,
                text="Thanks for your comment!",
            )

            assert result.status == ActionStatus.SENT
            assert result.external_id == "reply_9988"


@pytest.mark.asyncio
async def test_public_reply_facebook(test_settings: Settings) -> None:
    """Facebook public reply posts to /{comment-id}/comments."""
    comment_id = "10001_comment_555"
    url = f"{test_settings.GRAPH_BASE_URL}/{test_settings.GRAPH_VERSION}/{comment_id}/comments"

    async with httpx.AsyncClient() as http_client:
        graph = GraphClient(settings=test_settings, http_client=http_client)
        sender = MetaMessageSender(graph_client=graph, settings=test_settings)

        with respx.mock(assert_all_called=True) as respx_mock:
            respx_mock.post(url).respond(status_code=200, json={"id": "fb_reply_1122"})

            result = await sender.public_reply(
                platform=Platform.FACEBOOK,
                comment_id=comment_id,
                text="Thanks for checking out our page!",
            )

            assert result.status == ActionStatus.SENT
            assert result.external_id == "fb_reply_1122"


@pytest.mark.asyncio
async def test_private_reply_graph_api(test_settings: Settings) -> None:
    """Private reply posts to /{PAGE_ID}/messages with recipient.comment_id."""
    page_id = test_settings.PAGE_ID
    url = f"{test_settings.GRAPH_BASE_URL}/{test_settings.GRAPH_VERSION}/{page_id}/messages"

    async with httpx.AsyncClient() as http_client:
        graph = GraphClient(settings=test_settings, http_client=http_client)
        sender = MetaMessageSender(graph_client=graph, settings=test_settings)

        with respx.mock(assert_all_called=True) as respx_mock:
            route = respx_mock.post(url).respond(
                status_code=200,
                json={"message_id": "mid_priv_7766"},
            )

            result = await sender.private_reply(
                page_id=page_id,
                comment_id="comment_123",
                text="Hi! Automated message here.",
            )

            assert result.status == ActionStatus.SENT
            assert result.external_id == "mid_priv_7766"
            assert route.call_count == 1


@pytest.mark.asyncio
async def test_send_dm_graph_api(test_settings: Settings) -> None:
    """Two-way direct message posts to /{PAGE_ID}/messages with recipient.id."""
    page_id = test_settings.PAGE_ID
    url = f"{test_settings.GRAPH_BASE_URL}/{test_settings.GRAPH_VERSION}/{page_id}/messages"

    async with httpx.AsyncClient() as http_client:
        graph = GraphClient(settings=test_settings, http_client=http_client)
        sender = MetaMessageSender(graph_client=graph, settings=test_settings)

        with respx.mock(assert_all_called=True) as respx_mock:
            respx_mock.post(url).respond(
                status_code=200,
                json={"message_id": "mid_dm_3344"},
            )

            result = await sender.send_dm(
                recipient_id="user_8899",
                text="Direct reply inside 24h window",
            )

            assert result.status == ActionStatus.SENT
            assert result.external_id == "mid_dm_3344"


@pytest.mark.asyncio
async def test_demo_mode_skips_http_calls(test_settings: Settings) -> None:
    """When DEMO_MODE is true, sender returns SENT without making external network calls."""
    demo_settings = test_settings.model_copy(update={"DEMO_MODE": True})

    async with httpx.AsyncClient() as http_client:
        graph = GraphClient(settings=demo_settings, http_client=http_client)
        sender = MetaMessageSender(graph_client=graph, settings=demo_settings)

        # No respx mock needed since no HTTP call is executed
        pub_res = await sender.public_reply(
            platform=Platform.INSTAGRAM,
            comment_id="ig_c_1",
            text="Demo public reply",
        )
        assert pub_res.status == ActionStatus.SENT
        assert pub_res.raw_status == "demo_sent"

        priv_res = await sender.private_reply(
            page_id=demo_settings.PAGE_ID,
            comment_id="ig_c_1",
            text="Demo private reply",
        )
        assert priv_res.status == ActionStatus.SENT
        assert priv_res.raw_status == "demo_sent"


@pytest.mark.asyncio
async def test_sender_maps_meta_window_error(test_settings: Settings) -> None:
    """When Meta returns subcode 2018001 or 2018047, MetaWindowError is raised."""
    page_id = test_settings.PAGE_ID
    url = f"{test_settings.GRAPH_BASE_URL}/{test_settings.GRAPH_VERSION}/{page_id}/messages"

    async with httpx.AsyncClient() as http_client:
        graph = GraphClient(settings=test_settings, http_client=http_client)
        sender = MetaMessageSender(graph_client=graph, settings=test_settings)

        with respx.mock(assert_all_called=True) as respx_mock:
            respx_mock.post(url).respond(
                status_code=400,
                json={
                    "error": {
                        "message": "Private reply already sent for this comment",
                        "code": 100,
                        "error_subcode": 2018001,
                    }
                },
            )

            with pytest.raises(MetaWindowError):
                await sender.private_reply(
                    page_id=page_id,
                    comment_id="comment_already_sent",
                    text="Hello",
                )


@pytest.mark.asyncio
async def test_sender_maps_meta_auth_error(test_settings: Settings) -> None:
    """When Meta returns code 190, MetaAuthError is raised."""
    page_id = test_settings.PAGE_ID
    url = f"{test_settings.GRAPH_BASE_URL}/{test_settings.GRAPH_VERSION}/{page_id}/messages"

    async with httpx.AsyncClient() as http_client:
        graph = GraphClient(settings=test_settings, http_client=http_client)
        sender = MetaMessageSender(graph_client=graph, settings=test_settings)

        with respx.mock(assert_all_called=True) as respx_mock:
            respx_mock.post(url).respond(
                status_code=400,
                json={
                    "error": {
                        "message": "Invalid OAuth access token",
                        "code": 190,
                    }
                },
            )

            with pytest.raises(MetaAuthError):
                await sender.private_reply(
                    page_id=page_id,
                    comment_id="comment_123",
                    text="Hello",
                )
