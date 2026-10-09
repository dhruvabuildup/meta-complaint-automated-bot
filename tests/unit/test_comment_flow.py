"""Unit and flow integration tests for CommentFlowService orchestrating end-to-end comment lifecycle."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import fakeredis.aioredis
import pytest

from meta_bot.config import Settings
from meta_bot.domain.enums import (
    ActionStatus,
    IntentGroup,
    Platform,
    SendKind,
)
from meta_bot.domain.models import CommentEvent
from meta_bot.infra.db.models import ActionModel, CommentModel, ContactModel
from meta_bot.services.comment_flow import CommentFlowService
from meta_bot.services.decide import CommentDecisionService
from meta_bot.workers.send_worker import SendWorker


class FixedClock:
    def __init__(self, t: datetime) -> None:
        self._t = t

    def now(self) -> datetime:
        return self._t


@pytest.fixture
def flow_clock() -> FixedClock:
    return FixedClock(datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_comment_flow_normal_inquiry(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
    flow_clock: FixedClock,
) -> None:
    """Normal product price inquiry generates decision, attaches texts, and queues in Redis."""
    decision_service = CommentDecisionService(
        settings=test_settings,
        random_source=lambda: 0.05,  # Force public reply
    )
    flow = CommentFlowService(
        decision_service=decision_service,
        redis_client=fake_redis,
        settings=test_settings,
        clock=flow_clock,
        delay_source=lambda _min, _max: 30.0,
    )

    contact_id = uuid4()
    comment_db_id = uuid4()
    claimed_action_id = uuid4()

    mock_contact = ContactModel(
        id=contact_id,
        platform=Platform.INSTAGRAM.value,
        external_id="customer_101",
        opted_out=False,
    )
    mock_comment = CommentModel(
        id=comment_db_id,
        platform=Platform.INSTAGRAM.value,
        comment_id="ig_c_101",
        post_id="post_999",
        commenter_external_id="customer_101",
        text="How much does this cost?",
        created_at=flow_clock.now(),
    )
    mock_claimed_action = ActionModel(
        id=claimed_action_id,
        platform=Platform.INSTAGRAM.value,
        kind=SendKind.PRIVATE_REPLY.value,
        contact_id=contact_id,
        comment_id=comment_db_id,
        status=ActionStatus.QUEUED.value,
        body_hash="",
        scheduled_at=flow_clock.now(),
    )

    mock_session = AsyncMock()
    mock_session.execute.side_effect = [
        MagicMock(scalar_one_or_none=lambda: mock_contact),
        MagicMock(scalar_one_or_none=lambda: mock_comment),
        MagicMock(scalar_one_or_none=lambda: mock_claimed_action),
        MagicMock(scalar_one=lambda: uuid4()),  # public reply action insert
    ]

    event = CommentEvent(
        platform=Platform.INSTAGRAM,
        comment_id="ig_c_101",
        post_id="post_999",
        commenter_id="customer_101",
        text="How much does this cost?",
        created_at=flow_clock.now(),
    )

    plan = await flow.handle_comment(session=mock_session, event=event)

    assert plan.is_handed_off is False
    assert plan.intent_group == IntentGroup.PRICE
    assert plan.public_reply_text is not None
    assert plan.private_reply_text is not None

    # Claimed action was updated with text and paced schedule (now + 30s)
    assert mock_claimed_action.payload_text == plan.private_reply_text
    assert mock_claimed_action.scheduled_at == datetime(
        2026, 4, 15, 12, 0, 30, tzinfo=timezone.utc
    )

    # Both private and public reply were added to Redis sorted set
    sends_count = await fake_redis.zcard("mb:v1:queue:sends")
    assert sends_count == 2


@pytest.mark.asyncio
async def test_comment_flow_complaint_zero_sends_and_handoff(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
    flow_clock: FixedClock,
) -> None:
    """Complaint comment triggers handoff and marks claimed action as SKIPPED with zero sends."""
    decision_service = CommentDecisionService(settings=test_settings)
    flow = CommentFlowService(
        decision_service=decision_service,
        redis_client=fake_redis,
        settings=test_settings,
        clock=flow_clock,
    )

    contact_id = uuid4()
    comment_db_id = uuid4()
    claimed_action_id = uuid4()

    mock_contact = ContactModel(
        id=contact_id,
        platform=Platform.FACEBOOK.value,
        external_id="customer_angry",
        opted_out=False,
    )
    mock_comment = CommentModel(
        id=comment_db_id,
        platform=Platform.FACEBOOK.value,
        comment_id="fb_c_202",
        post_id="fb_p_888",
        commenter_external_id="customer_angry",
        text="I want an immediate refund, this is fraud!",
        created_at=flow_clock.now(),
    )
    mock_claimed_action = ActionModel(
        id=claimed_action_id,
        platform=Platform.FACEBOOK.value,
        kind=SendKind.PRIVATE_REPLY.value,
        contact_id=contact_id,
        comment_id=comment_db_id,
        status=ActionStatus.QUEUED.value,
        body_hash="",
        scheduled_at=flow_clock.now(),
    )

    mock_session = AsyncMock()
    mock_session.execute.side_effect = [
        MagicMock(scalar_one_or_none=lambda: mock_contact),
        MagicMock(scalar_one_or_none=lambda: mock_comment),
        MagicMock(scalar_one_or_none=lambda: mock_claimed_action),
        MagicMock(),  # conversation insert
    ]

    event = CommentEvent(
        platform=Platform.FACEBOOK,
        comment_id="fb_c_202",
        post_id="fb_p_888",
        commenter_id="customer_angry",
        text="I want an immediate refund, this is fraud!",
        created_at=flow_clock.now(),
    )

    plan = await flow.handle_comment(session=mock_session, event=event)

    assert plan.is_handed_off is True
    assert plan.public_reply_text is None
    assert plan.private_reply_text is None

    # Claimed action was marked SKIPPED
    assert mock_claimed_action.status == ActionStatus.SKIPPED.value
    assert mock_claimed_action.error_code == "COMPLAINT"

    # Zero items scheduled in Redis sorted set!
    assert await fake_redis.zcard("mb:v1:queue:sends") == 0


@pytest.mark.asyncio
async def test_end_to_end_demo_mode_send_execution(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
    flow_clock: FixedClock,
) -> None:
    """In DEMO_MODE=True, the worker executes and marks actions as SENT with mock external IDs."""
    demo_settings = test_settings.model_copy(update={"DEMO_MODE": True})

    action_id = uuid4()
    await fake_redis.zadd(
        "mb:v1:queue:sends", {str(action_id): flow_clock.now().timestamp() - 5}
    )

    mock_action = ActionModel(
        id=action_id,
        platform=Platform.INSTAGRAM.value,
        kind=SendKind.PRIVATE_REPLY.value,
        contact_id=None,
        comment_id=uuid4(),
        payload_text="Demo message text",
        body_hash="hash",
        status=ActionStatus.QUEUED.value,
        scheduled_at=flow_clock.now() - timedelta(seconds=5),
        attempts=0,
    )
    mock_comment = CommentModel(
        id=mock_action.comment_id,
        platform=Platform.INSTAGRAM.value,
        comment_id="demo_c_123",
        post_id="demo_p_123",
        commenter_external_id="user_demo",
        text="pricing",
        created_at=flow_clock.now() - timedelta(minutes=2),
    )

    mock_session = AsyncMock()
    mock_session.execute.side_effect = [
        MagicMock(scalar_one_or_none=lambda: mock_action),
        MagicMock(scalar_one_or_none=lambda: mock_comment),
        MagicMock(),  # update status
    ]
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session

    worker = SendWorker(
        settings=demo_settings,
        session_factory=mock_session_factory,
        redis_client=fake_redis,
        clock=flow_clock,
    )

    executed = await worker.poll_due_actions()
    assert executed == 1

    # Verify action was removed from Redis queue
    assert await fake_redis.zscore("mb:v1:queue:sends", str(action_id)) is None
