"""Paced outbound message scheduler pacing sends using Redis sorted sets and jitter."""

import hashlib
import random
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

import structlog
from redis.asyncio import Redis

from meta_bot.config import Settings, get_settings
from meta_bot.domain.enums import Platform, SendKind
from meta_bot.domain.models import Clock, SystemClock
from meta_bot.infra.redis.keys import send_queue_key
from meta_bot.services.ports import ActionRepository

logger = structlog.get_logger(__name__)


def compute_body_hash(text: str) -> str:
    """Compute sha256 hash of text for deduplication."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32]


class SendScheduler:
    """Schedules outbound actions with jitter into a Redis sorted set queue.

    Meta rule: Paced with random delay (20 to 60 seconds) for first DMs to avoid spam flags.
    """

    def __init__(
        self,
        action_repo: ActionRepository,
        redis_client: Redis,
        settings: Settings | None = None,
        clock: Clock | None = None,
        delay_source: Callable[[float, float], float] | None = None,
    ) -> None:
        self.action_repo = action_repo
        self.redis = redis_client
        self.settings = settings or get_settings()
        self.clock = clock or SystemClock()
        self.delay_source = delay_source or random.uniform
        self.queue_key = send_queue_key()

    async def schedule_private_reply(
        self,
        platform: Platform,
        contact_id: UUID,
        comment_id: UUID,
        text: str,
        template_id: UUID | None = None,
        conversation_id: UUID | None = None,
    ) -> UUID:
        """Schedule a private reply with random pacing delay between 20 and 60 seconds."""
        delay = self.delay_source(
            float(self.settings.SEND_DELAY_MIN_SECONDS),
            float(self.settings.SEND_DELAY_MAX_SECONDS),
        )
        scheduled_at = self.clock.now() + timedelta(seconds=delay)
        body_hash = compute_body_hash(text)

        action_id = await self.action_repo.create_action(
            platform=platform,
            kind=SendKind.PRIVATE_REPLY,
            contact_id=contact_id,
            comment_id=comment_id,
            body_hash=body_hash,
            payload_text=text,
            scheduled_at=scheduled_at,
            template_id=template_id,
            conversation_id=conversation_id,
        )

        # Enqueue into Redis sorted set scored by scheduled epoch timestamp
        await self.redis.zadd(
            self.queue_key, {str(action_id): scheduled_at.timestamp()}
        )
        logger.info(
            "Private reply scheduled",
            action_id=str(action_id),
            delay_seconds=round(delay, 2),
            scheduled_at=scheduled_at.isoformat(),
        )
        return action_id

    async def schedule_public_reply(
        self,
        platform: Platform,
        contact_id: UUID,
        comment_id: UUID,
        text: str,
        template_id: UUID | None = None,
    ) -> UUID:
        """Schedule a public comment reply with moderate jitter delay."""
        delay = self.delay_source(3.0, 12.0)
        scheduled_at = self.clock.now() + timedelta(seconds=delay)
        body_hash = compute_body_hash(text)

        action_id = await self.action_repo.create_action(
            platform=platform,
            kind=SendKind.PUBLIC_REPLY,
            contact_id=contact_id,
            comment_id=comment_id,
            body_hash=body_hash,
            payload_text=text,
            scheduled_at=scheduled_at,
            template_id=template_id,
        )

        await self.redis.zadd(
            self.queue_key, {str(action_id): scheduled_at.timestamp()}
        )
        logger.info(
            "Public reply scheduled",
            action_id=str(action_id),
            delay_seconds=round(delay, 2),
            scheduled_at=scheduled_at.isoformat(),
        )
        return action_id

    async def schedule_dm(
        self,
        platform: Platform,
        contact_id: UUID,
        text: str,
        conversation_id: UUID | None = None,
    ) -> UUID:
        """Schedule a standard two-way DM with fast pacing jitter."""
        delay = self.delay_source(1.0, 4.0)
        scheduled_at = self.clock.now() + timedelta(seconds=delay)
        body_hash = compute_body_hash(text)

        action_id = await self.action_repo.create_action(
            platform=platform,
            kind=SendKind.DM,
            contact_id=contact_id,
            comment_id=None,
            body_hash=body_hash,
            payload_text=text,
            scheduled_at=scheduled_at,
            conversation_id=conversation_id,
        )

        await self.redis.zadd(
            self.queue_key, {str(action_id): scheduled_at.timestamp()}
        )
        logger.info(
            "Direct message scheduled",
            action_id=str(action_id),
            delay_seconds=round(delay, 2),
        )
        return action_id
