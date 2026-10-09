"""Unit tests for SendWorker queue consumer, retry backoff, error handling, and recovery."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import fakeredis.aioredis
import pytest

from meta_bot.config import Settings
from meta_bot.domain.enums import ActionStatus, GuardReason, Platform, SendKind
from meta_bot.domain.models import GuardDecision, SendResult
from meta_bot.errors import (
    MetaTransientError,
    MetaWindowError,
)
from meta_bot.infra.db.models import ActionModel, CommentModel, ContactModel
from meta_bot.services.ports import MessageSender
from meta_bot.services.send_guard import SendGuard
from meta_bot.workers.send_worker import SendWorker


class FixedClock:
    def __init__(self, t: datetime) -> None:
        self._t = t

    def now(self) -> datetime:
        return self._t


@pytest.fixture
def test_clock() -> FixedClock:
    return FixedClock(datetime(2026, 4, 15, 14, 0, 0, tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_send_worker_executes_due_action(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
    test_clock: FixedClock,
) -> None:
    """Due action in Redis sorted set is claimed, validated by guard, and sent."""
    action_id = uuid4()
    contact_id = uuid4()
    comment_id = uuid4()

    # Seed action in Redis ZSET with past timestamp (due now)
    await fake_redis.zadd(
        "mb:v1:queue:sends", {str(action_id): test_clock.now().timestamp() - 10}
    )

    # Prepare mock DB entities
    mock_action = ActionModel(
        id=action_id,
        platform=Platform.INSTAGRAM.value,
        kind=SendKind.PRIVATE_REPLY.value,
        contact_id=contact_id,
        comment_id=comment_id,
        payload_text="Here is your private reply",
        body_hash="hash123",
        status=ActionStatus.QUEUED.value,
        scheduled_at=test_clock.now() - timedelta(seconds=10),
        attempts=0,
    )
    mock_contact = ContactModel(
        id=contact_id,
        platform=Platform.INSTAGRAM.value,
        external_id="user_test_99",
        opted_out=False,
    )
    mock_comment = CommentModel(
        id=comment_id,
        platform=Platform.INSTAGRAM.value,
        comment_id="comment_ext_88",
        post_id="post_ext_77",
        commenter_external_id="user_test_99",
        text="Interested in this",
        created_at=test_clock.now() - timedelta(minutes=5),
    )

    # Mock DB session
    mock_session = AsyncMock()
    mock_session.execute.side_effect = [
        MagicMock(scalar_one_or_none=lambda: mock_action),
        MagicMock(scalar_one_or_none=lambda: mock_contact),
        MagicMock(scalar_one_or_none=lambda: mock_comment),
        MagicMock(),  # update action status
    ]

    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session

    mock_sender = AsyncMock(spec=MessageSender)
    mock_sender.private_reply.return_value = SendResult(
        status=ActionStatus.SENT,
        external_id="meta_msg_112233",
    )

    mock_guard = AsyncMock(spec=SendGuard)
    mock_guard.check.return_value = GuardDecision(
        allowed=True, reason=GuardReason.ALLOWED
    )

    worker = SendWorker(
        settings=test_settings,
        session_factory=mock_session_factory,
        redis_client=fake_redis,
        sender=mock_sender,
        guard=mock_guard,
        clock=test_clock,
    )

    executed = await worker.poll_due_actions()
    assert executed == 1
    mock_sender.private_reply.assert_called_once()
    mock_guard.record_send_success.assert_called_once()

    # Item must be removed from Redis sorted set
    score = await fake_redis.zscore("mb:v1:queue:sends", str(action_id))
    assert score is None


@pytest.mark.asyncio
async def test_send_worker_reschedules_on_rate_cap(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
    test_clock: FixedClock,
) -> None:
    """When rate cap is hit, action is re-scheduled into future, NOT dropped or skipped."""
    action_id = uuid4()
    await fake_redis.zadd(
        "mb:v1:queue:sends", {str(action_id): test_clock.now().timestamp()}
    )

    mock_action = ActionModel(
        id=action_id,
        platform=Platform.INSTAGRAM.value,
        kind=SendKind.PRIVATE_REPLY.value,
        contact_id=None,
        comment_id=None,
        payload_text="Rate capped DM",
        body_hash="hash123",
        status=ActionStatus.QUEUED.value,
        scheduled_at=test_clock.now(),
        attempts=0,
    )

    mock_session = AsyncMock()
    mock_session.execute.side_effect = [
        MagicMock(scalar_one_or_none=lambda: mock_action),
    ]
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session

    mock_sender = AsyncMock(spec=MessageSender)
    mock_guard = AsyncMock(spec=SendGuard)
    mock_guard.check.return_value = GuardDecision(
        allowed=False,
        reason=GuardReason.HOURLY_CAP_REACHED,
        retry_after_seconds=1200,  # 20 minutes
    )

    worker = SendWorker(
        settings=test_settings,
        session_factory=mock_session_factory,
        redis_client=fake_redis,
        sender=mock_sender,
        guard=mock_guard,
        clock=test_clock,
    )

    executed = await worker.poll_due_actions()
    assert executed == 1
    # Sender was NOT called
    mock_sender.private_reply.assert_not_called()

    # Action was re-added to Redis sorted set with future score (now + 1200s)
    expected_score = test_clock.now().timestamp() + 1200
    new_score = await fake_redis.zscore("mb:v1:queue:sends", str(action_id))
    assert new_score == pytest.approx(expected_score, abs=1e-3)


@pytest.mark.asyncio
async def test_send_worker_skips_permanently_on_window_error(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
    test_clock: FixedClock,
) -> None:
    """MetaWindowError marks action permanently SKIPPED, never retried."""
    action_id = uuid4()
    await fake_redis.zadd(
        "mb:v1:queue:sends", {str(action_id): test_clock.now().timestamp()}
    )

    mock_action = ActionModel(
        id=action_id,
        platform=Platform.FACEBOOK.value,
        kind=SendKind.PRIVATE_REPLY.value,
        contact_id=None,
        comment_id=uuid4(),
        payload_text="Window expired DM",
        body_hash="hash123",
        status=ActionStatus.QUEUED.value,
        scheduled_at=test_clock.now(),
        attempts=0,
    )
    mock_comment = CommentModel(
        id=mock_action.comment_id,
        platform=Platform.FACEBOOK.value,
        comment_id="comment_expired_1",
        post_id="post_1",
        commenter_external_id="user_1",
        text="old comment",
        created_at=test_clock.now() - timedelta(days=8),
    )

    mock_session = AsyncMock()
    mock_session.execute.side_effect = [
        MagicMock(scalar_one_or_none=lambda: mock_action),
        MagicMock(scalar_one_or_none=lambda: mock_comment),
        MagicMock(),  # update status
    ]
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session

    mock_sender = AsyncMock(spec=MessageSender)
    mock_sender.private_reply.side_effect = MetaWindowError(
        status_code=400,
        meta_subcode=2018047,
        message="Cannot send private reply: older than 7 days",
    )

    mock_guard = AsyncMock(spec=SendGuard)
    mock_guard.check.return_value = GuardDecision(
        allowed=True, reason=GuardReason.ALLOWED
    )

    worker = SendWorker(
        settings=test_settings,
        session_factory=mock_session_factory,
        redis_client=fake_redis,
        sender=mock_sender,
        guard=mock_guard,
        clock=test_clock,
    )

    executed = await worker.poll_due_actions()
    assert executed == 1

    # Not re-queued in Redis
    assert await fake_redis.zscore("mb:v1:queue:sends", str(action_id)) is None


@pytest.mark.asyncio
async def test_send_worker_transient_error_retry_and_dead_letter(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
    test_clock: FixedClock,
) -> None:
    """Transient error retries with backoff; exceeds max attempts marks DEAD."""
    action_id = uuid4()
    await fake_redis.zadd(
        "mb:v1:queue:sends", {str(action_id): test_clock.now().timestamp()}
    )

    # Action on attempt 2 (max attempts is 3)
    mock_action = ActionModel(
        id=action_id,
        platform=Platform.INSTAGRAM.value,
        kind=SendKind.DM.value,
        contact_id=None,
        comment_id=None,
        payload_text="Retry test DM",
        body_hash="hash123",
        status=ActionStatus.QUEUED.value,
        scheduled_at=test_clock.now(),
        attempts=2,
    )

    mock_session = AsyncMock()
    mock_session.execute.side_effect = [
        MagicMock(scalar_one_or_none=lambda: mock_action),
        MagicMock(),  # update action status
    ]
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session

    mock_sender = AsyncMock(spec=MessageSender)
    mock_sender.send_dm.side_effect = MetaTransientError(
        status_code=503, message="Timeout"
    )

    mock_guard = AsyncMock(spec=SendGuard)
    mock_guard.check.return_value = GuardDecision(
        allowed=True, reason=GuardReason.ALLOWED
    )

    worker = SendWorker(
        settings=test_settings,
        session_factory=mock_session_factory,
        redis_client=fake_redis,
        sender=mock_sender,
        guard=mock_guard,
        clock=test_clock,
        max_transient_attempts=3,
    )

    # 3rd attempt fails -> marked DEAD
    executed = await worker.poll_due_actions()
    assert executed == 1

    # Not re-queued because attempts >= max_transient_attempts
    assert await fake_redis.zscore("mb:v1:queue:sends", str(action_id)) is None
