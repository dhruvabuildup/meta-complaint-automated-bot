"""Unit tests for SendScheduler pacing delays and Redis sorted set queuing."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import fakeredis.aioredis
import pytest

from meta_bot.config import Settings
from meta_bot.domain.enums import ActionStatus, Platform, SendKind
from meta_bot.services.ports import ActionRepository
from meta_bot.services.send_scheduler import SendScheduler


class InMemoryActionRepo(ActionRepository):
    """In-memory test action repository."""

    def __init__(self) -> None:
        self.actions: dict[UUID, dict[str, Any]] = {}

    async def claim_private_reply(
        self,
        platform: Platform,
        contact_id: UUID,
        comment_id: UUID,
        body_hash: str,
        scheduled_at: datetime | None = None,
    ) -> bool:
        return True

    async def create_action(
        self,
        platform: Platform,
        kind: SendKind,
        contact_id: UUID | None,
        comment_id: UUID | None,
        body_hash: str,
        payload_text: str,
        scheduled_at: datetime,
        template_id: UUID | None = None,
        conversation_id: UUID | None = None,
    ) -> UUID:
        aid = uuid4()
        self.actions[aid] = {
            "id": aid,
            "platform": platform,
            "kind": kind,
            "contact_id": contact_id,
            "comment_id": comment_id,
            "body_hash": body_hash,
            "payload_text": payload_text,
            "scheduled_at": scheduled_at,
            "status": ActionStatus.QUEUED,
        }
        return aid

    async def update_action_status(
        self,
        action_id: UUID,
        status: ActionStatus,
        sent_at: datetime | None = None,
        external_id: str | None = None,
        error_code: str | None = None,
        increment_attempts: bool = False,
    ) -> None:
        if action_id in self.actions:
            self.actions[action_id]["status"] = status
            if external_id:
                self.actions[action_id]["external_id"] = external_id


class FixedClock:
    def __init__(self, t: datetime) -> None:
        self._t = t

    def now(self) -> datetime:
        return self._t


@pytest.mark.asyncio
async def test_schedule_private_reply_paces_with_delay(
    fake_redis: fakeredis.aioredis.FakeRedis,
    test_settings: Settings,
) -> None:
    """Private reply delay is sampled within SEND_DELAY_MIN and SEND_DELAY_MAX and enqueued into Redis."""
    repo = InMemoryActionRepo()
    now = datetime(2026, 4, 15, 10, 0, 0, tzinfo=timezone.utc)
    clock = FixedClock(now)

    # Inject fixed delay 45.0 seconds
    scheduler = SendScheduler(
        action_repo=repo,
        redis_client=fake_redis,
        settings=test_settings,
        clock=clock,
        delay_source=lambda _min, _max: 45.0,
    )

    contact_id = uuid4()
    comment_id = uuid4()
    action_id = await scheduler.schedule_private_reply(
        platform=Platform.INSTAGRAM,
        contact_id=contact_id,
        comment_id=comment_id,
        text="Hello in private DM",
    )

    assert action_id in repo.actions
    saved = repo.actions[action_id]
    assert saved["kind"] == SendKind.PRIVATE_REPLY
    assert saved["payload_text"] == "Hello in private DM"
    assert saved["scheduled_at"] == datetime(
        2026, 4, 15, 10, 0, 45, tzinfo=timezone.utc
    )

    # Check Redis sorted set score
    score = await fake_redis.zscore("mb:v1:queue:sends", str(action_id))
    assert score == pytest.approx(saved["scheduled_at"].timestamp(), abs=1e-3)


@pytest.mark.asyncio
async def test_schedule_public_reply_adds_jitter(
    fake_redis: fakeredis.aioredis.FakeRedis,
    test_settings: Settings,
) -> None:
    """Public reply delay uses moderate jitter and is enqueued into Redis sorted set."""
    repo = InMemoryActionRepo()
    now = datetime(2026, 4, 15, 10, 0, 0, tzinfo=timezone.utc)
    clock = FixedClock(now)

    scheduler = SendScheduler(
        action_repo=repo,
        redis_client=fake_redis,
        settings=test_settings,
        clock=clock,
        delay_source=lambda _min, _max: 8.5,
    )

    contact_id = uuid4()
    comment_id = uuid4()
    action_id = await scheduler.schedule_public_reply(
        platform=Platform.FACEBOOK,
        contact_id=contact_id,
        comment_id=comment_id,
        text="Public comment response",
    )

    assert action_id in repo.actions
    saved = repo.actions[action_id]
    assert saved["kind"] == SendKind.PUBLIC_REPLY
    assert saved["scheduled_at"] == datetime(
        2026, 4, 15, 10, 0, 8, 500000, tzinfo=timezone.utc
    )

    score = await fake_redis.zscore("mb:v1:queue:sends", str(action_id))
    assert score is not None
