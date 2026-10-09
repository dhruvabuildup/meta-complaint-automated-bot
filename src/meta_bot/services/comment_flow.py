"""Orchestrator wiring event ingestion pipeline to comment decision and action scheduler."""

import hashlib
import random
from collections.abc import Callable
from datetime import timedelta

import structlog
from redis.asyncio import Redis
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from meta_bot.config import Settings, get_settings
from meta_bot.domain.enums import ActionStatus, ConversationState, SendKind
from meta_bot.domain.models import Clock, CommentEvent, DecisionPlan, SystemClock
from meta_bot.infra.db.models import (
    ActionModel,
    CommentModel,
    ContactModel,
    ConversationModel,
)
from meta_bot.infra.redis.keys import post_last_variant_key, send_queue_key
from meta_bot.services.decide import CommentDecisionService

logger = structlog.get_logger(__name__)


def compute_body_hash(text: str) -> str:
    """Compute sha256 hash of text for deduplication."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32]


class CommentFlowService:
    """Orchestrates decision making and action scheduling for validated incoming comments."""

    def __init__(
        self,
        decision_service: CommentDecisionService,
        redis_client: Redis,
        settings: Settings | None = None,
        clock: Clock | None = None,
        delay_source: Callable[[float, float], float] | None = None,
    ) -> None:
        self.decision_service = decision_service
        self.redis = redis_client
        self.settings = settings or get_settings()
        self.clock = clock or SystemClock()
        self.delay_source = delay_source or random.uniform
        self.queue_key = send_queue_key()

    async def handle_comment(
        self,
        session: AsyncSession,
        event: CommentEvent,
    ) -> DecisionPlan:
        """Run decision logic on comment, attach texts to actions, and schedule in Redis."""
        now = self.clock.now()

        # 1. Fetch persisted contact and comment
        contact_res = await session.execute(
            select(ContactModel).where(
                ContactModel.platform == event.platform.value,
                ContactModel.external_id == event.commenter_id,
            )
        )
        contact = contact_res.scalar_one_or_none()

        comment_res = await session.execute(
            select(CommentModel).where(CommentModel.comment_id == event.comment_id)
        )
        comment = comment_res.scalar_one_or_none()

        # 2. Retrieve last variant posted to this post for rotation
        last_variant_key = post_last_variant_key(event.post_id)
        last_var_raw = await self.redis.get(last_variant_key)
        last_variant = (
            last_var_raw.decode("utf-8")
            if isinstance(last_var_raw, bytes)
            else (str(last_var_raw) if last_var_raw else None)
        )

        # 3. Evaluate comment through decision layer
        plan = self.decision_service.decide(
            event=event,
            last_public_variant=last_variant,
            account_name="our team",
        )

        # 4. Check for claimed private reply action
        claimed_action: ActionModel | None = None
        if comment:
            act_res = await session.execute(
                select(ActionModel).where(
                    ActionModel.comment_id == comment.id,
                    ActionModel.kind == SendKind.PRIVATE_REPLY.value,
                )
            )
            claimed_action = act_res.scalar_one_or_none()

        # 5. Handle Complaints / Handoff
        if plan.is_handed_off:
            logger.warning(
                "Comment requires human handoff; suppressing automated replies",
                comment_id=event.comment_id,
                reason=plan.handoff_reason.value if plan.handoff_reason else "unknown",
            )
            if contact:
                # Record handoff conversation state
                stmt = insert(ConversationModel).values(
                    platform=event.platform.value,
                    contact_id=contact.id,
                    state=ConversationState.HANDED_OFF.value,
                    handoff_reason=(
                        plan.handoff_reason.value
                        if plan.handoff_reason
                        else "COMPLAINT"
                    ),
                )
                await session.execute(stmt)

            if claimed_action:
                # Mark claimed action as skipped due to handoff
                claimed_action.status = ActionStatus.SKIPPED.value
                claimed_action.error_code = (
                    plan.handoff_reason.value if plan.handoff_reason else "HANDED_OFF"
                )

            return plan

        # 6. Schedule Private Reply (with 20 to 60s random delay)
        if plan.private_reply_text and claimed_action:
            delay = self.delay_source(
                float(self.settings.SEND_DELAY_MIN_SECONDS),
                float(self.settings.SEND_DELAY_MAX_SECONDS),
            )
            scheduled_at = now + timedelta(seconds=delay)
            body_hash = compute_body_hash(plan.private_reply_text)

            claimed_action.payload_text = plan.private_reply_text
            claimed_action.body_hash = body_hash
            claimed_action.scheduled_at = scheduled_at
            claimed_action.status = ActionStatus.QUEUED.value

            # Enqueue into Redis sorted set
            await self.redis.zadd(
                self.queue_key,
                {str(claimed_action.id): scheduled_at.timestamp()},
            )
            logger.info(
                "Paced private reply enqueued in Redis",
                action_id=str(claimed_action.id),
                delay_seconds=round(delay, 2),
            )

        # 7. Schedule Public Reply (if selected)
        if plan.public_reply_text and comment:
            pub_delay = self.delay_source(3.0, 12.0)
            pub_scheduled_at = now + timedelta(seconds=pub_delay)
            pub_body_hash = compute_body_hash(plan.public_reply_text)

            pub_action_stmt = (
                insert(ActionModel)
                .values(
                    platform=event.platform.value,
                    kind=SendKind.PUBLIC_REPLY.value,
                    contact_id=contact.id if contact else None,
                    comment_id=comment.id,
                    body_hash=pub_body_hash,
                    payload_text=plan.public_reply_text,
                    scheduled_at=pub_scheduled_at,
                    status=ActionStatus.QUEUED.value,
                )
                .returning(ActionModel.id)
            )
            pub_res = await session.execute(pub_action_stmt)
            pub_action_id = pub_res.scalar_one()

            # Enqueue public reply into Redis sorted set
            await self.redis.zadd(
                self.queue_key,
                {str(pub_action_id): pub_scheduled_at.timestamp()},
            )

            # Update last variant in Redis for rotation
            await self.redis.set(
                last_variant_key,
                plan.public_reply_text,
                ex=604800,  # 7 days
            )
            logger.info(
                "Paced public reply enqueued in Redis",
                action_id=str(pub_action_id),
                delay_seconds=round(pub_delay, 2),
            )

        return plan
