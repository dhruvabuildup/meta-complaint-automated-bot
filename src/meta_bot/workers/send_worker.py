"""Background worker consuming paced outbound actions from Redis sorted set."""

import asyncio
import signal
from datetime import timedelta
from typing import Any
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meta_bot.adapters.meta.graph_client import GraphClient
from meta_bot.adapters.meta.senders import MetaMessageSender
from meta_bot.config import Settings, get_settings
from meta_bot.domain.enums import ActionStatus, GuardReason, Platform, SendKind
from meta_bot.domain.models import Clock, SendContext, SystemClock
from meta_bot.errors import (
    MetaAuthError,
    MetaPermissionError,
    MetaRateLimitError,
    MetaTransientError,
    MetaWindowError,
)
from meta_bot.infra.db.engine import create_engine
from meta_bot.infra.db.models import ActionModel, CommentModel, ContactModel
from meta_bot.infra.db.repositories import SqlAlchemyActionRepository
from meta_bot.infra.db.session import create_session_factory
from meta_bot.infra.redis.client import get_redis_client
from meta_bot.infra.redis.keys import send_queue_key
from meta_bot.logging_setup import setup_logging
from meta_bot.services.ports import MessageSender
from meta_bot.services.send_guard import SendGuard

logger = structlog.get_logger(__name__)


class SendWorker:
    """Worker polling due outbound actions from Redis sorted set and executing sends safely."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: Redis,
        sender: MessageSender | None = None,
        guard: SendGuard | None = None,
        clock: Clock | None = None,
        max_transient_attempts: int = 3,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.redis = redis_client
        self.clock = clock or SystemClock()
        self.queue_key = send_queue_key()
        self.max_transient_attempts = max_transient_attempts
        self._stop_event = asyncio.Event()

        self.guard = guard or SendGuard(
            redis_client=self.redis,
            settings=self.settings,
            clock=self.clock,
        )

        if sender is None:
            graph_client = GraphClient(settings=self.settings)
            self.sender: MessageSender = MetaMessageSender(
                graph_client=graph_client,
                settings=self.settings,
            )
        else:
            self.sender = sender

    async def recover_pending_actions(self) -> int:
        """Crash safety: reconcile actions in DB marked QUEUED into Redis sorted set on startup."""
        now = self.clock.now()
        horizon = now + timedelta(hours=1)
        recovered = 0

        async with self.session_factory() as session:
            stmt = select(ActionModel).where(
                ActionModel.status == ActionStatus.QUEUED.value,
                ActionModel.scheduled_at <= horizon,
            )
            res = await session.execute(stmt)
            actions = res.scalars().all()

            for action in actions:
                score = action.scheduled_at.timestamp()
                # ZADD with NX (only add if not existing in set)
                added = await self.redis.zadd(
                    self.queue_key, {str(action.id): score}, nx=True
                )
                if added:
                    recovered += 1

        if recovered > 0:
            logger.info("Recovered orphaned actions on startup", count=recovered)
        return recovered

    async def execute_action(self, action_id: UUID) -> None:
        """Process a single due action through SendGuard and MessageSender."""
        async with self.session_factory() as session:
            repo = SqlAlchemyActionRepository(session)
            action = await repo.get_action(action_id)
            if not action or action.status != ActionStatus.QUEUED.value:
                return

            contact: ContactModel | None = None
            if action.contact_id:
                c_res = await session.execute(
                    select(ContactModel).where(ContactModel.id == action.contact_id)
                )
                contact = c_res.scalar_one_or_none()

            comment: CommentModel | None = None
            if action.comment_id:
                cm_res = await session.execute(
                    select(CommentModel).where(CommentModel.id == action.comment_id)
                )
                comment = cm_res.scalar_one_or_none()

            context = SendContext(
                account_id=self.settings.PAGE_ID,
                platform=Platform(action.platform),
                contact_id=contact.external_id if contact else "",
                comment_id=comment.comment_id if comment else None,
                post_id=comment.post_id if comment else None,
                comment_created_at=comment.created_at if comment else None,
                last_user_message_at=(
                    contact.last_user_message_at if contact else None
                ),
                is_opted_out=contact.opted_out if contact else False,
                send_kind=SendKind(action.kind),
                body=action.payload_text or "",
            )

            # Evaluate all compliance and safety rules via SendGuard
            guard_decision = await self.guard.check(context)

            if not guard_decision.allowed:
                if guard_decision.reason in (
                    GuardReason.HOURLY_CAP_REACHED,
                    GuardReason.DAILY_CAP_REACHED,
                ):
                    # Rate cap hit: re-schedule into future, do NOT drop
                    retry_seconds = guard_decision.retry_after_seconds or 300
                    new_time = self.clock.now() + timedelta(seconds=retry_seconds)
                    action.scheduled_at = new_time
                    await session.commit()

                    await self.redis.zadd(
                        self.queue_key, {str(action_id): new_time.timestamp()}
                    )
                    logger.info(
                        "Action re-scheduled due to rate cap",
                        action_id=str(action_id),
                        reason=guard_decision.reason.value,
                        retry_seconds=retry_seconds,
                    )
                    return

                # Other guard rejection (kill switch, window expired, opted out, duplicate body)
                await repo.update_action_status(
                    action_id=action_id,
                    status=ActionStatus.SKIPPED,
                    error_code=guard_decision.reason.value,
                )
                await session.commit()
                logger.info(
                    "Action skipped by send guard",
                    action_id=str(action_id),
                    reason=guard_decision.reason.value,
                )
                return

            # Execute send via MessageSender
            try:
                now_send = self.clock.now()
                if action.kind == SendKind.PUBLIC_REPLY.value:
                    if not context.comment_id:
                        raise ValueError("Missing comment_id for public reply")
                    res = await self.sender.public_reply(
                        platform=context.platform,
                        comment_id=context.comment_id,
                        text=context.body,
                    )
                elif action.kind == SendKind.PRIVATE_REPLY.value:
                    if not context.comment_id:
                        raise ValueError("Missing comment_id for private reply")
                    res = await self.sender.private_reply(
                        page_id=self.settings.PAGE_ID,
                        comment_id=context.comment_id,
                        text=context.body,
                    )
                else:  # SendKind.DM
                    res = await self.sender.send_dm(
                        recipient_id=context.contact_id,
                        text=context.body,
                    )

                # Send succeeded
                await self.guard.record_send_attempt_success()
                await self.guard.record_send_success(context)
                await repo.update_action_status(
                    action_id=action_id,
                    status=ActionStatus.SENT,
                    sent_at=res.sent_at or now_send,
                    external_id=res.external_id,
                    increment_attempts=True,
                )
                await session.commit()
                logger.info(
                    "Action successfully sent",
                    action_id=str(action_id),
                    kind=action.kind,
                    external_id=res.external_id,
                )

            except MetaWindowError as exc:
                # Meta rule: window expired or private reply already sent is a permanent skip, never retried
                logger.warning(
                    "Action permanently skipped: Meta window error",
                    action_id=str(action_id),
                    error=str(exc),
                )
                await repo.update_action_status(
                    action_id=action_id,
                    status=ActionStatus.SKIPPED,
                    error_code="META_WINDOW_EXPIRED",
                    increment_attempts=True,
                )
                await session.commit()

            except (MetaAuthError, MetaPermissionError) as exc:
                logger.error(
                    "Critical Meta error encountered",
                    action_id=str(action_id),
                    error=str(exc),
                )
                await self.guard.record_meta_error(exc)
                await repo.update_action_status(
                    action_id=action_id,
                    status=ActionStatus.FAILED,
                    error_code="META_AUTH_OR_PERMISSION_ERROR",
                    increment_attempts=True,
                )
                await session.commit()

            except MetaRateLimitError as exc:
                backoff = exc.retry_after or 60.0
                new_time = self.clock.now() + timedelta(seconds=backoff)
                action.scheduled_at = new_time
                action.attempts += 1
                await session.commit()
                await self.redis.zadd(
                    self.queue_key, {str(action_id): new_time.timestamp()}
                )
                logger.warning(
                    "Rate limited by Meta; re-scheduled action",
                    action_id=str(action_id),
                    backoff_seconds=backoff,
                )

            except MetaTransientError:
                attempts = action.attempts + 1
                if attempts < self.max_transient_attempts:
                    backoff = min(300.0, (2**attempts) * 5.0)
                    new_time = self.clock.now() + timedelta(seconds=backoff)
                    action.scheduled_at = new_time
                    action.attempts = attempts
                    await session.commit()
                    await self.redis.zadd(
                        self.queue_key, {str(action_id): new_time.timestamp()}
                    )
                    logger.warning(
                        "Transient Meta error; backing off",
                        action_id=str(action_id),
                        attempt=attempts,
                        backoff=backoff,
                    )
                else:
                    logger.critical(
                        "Action exceeded transient retry attempts; marked DEAD",
                        action_id=str(action_id),
                    )
                    await repo.update_action_status(
                        action_id=action_id,
                        status=ActionStatus.DEAD,
                        error_code="MAX_RETRIES_EXCEEDED",
                        increment_attempts=True,
                    )
                    await session.commit()

            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Unexpected error executing action",
                    action_id=str(action_id),
                    error=str(exc),
                )
                await repo.update_action_status(
                    action_id=action_id,
                    status=ActionStatus.FAILED,
                    error_code="UNEXPECTED_ERROR",
                    increment_attempts=True,
                )
                await session.commit()

    async def poll_due_actions(self) -> int:
        """Poll and execute batch of due items from sorted set."""
        now_ts = self.clock.now().timestamp()
        # Fetch items scheduled at or before now_ts
        due_items = await self.redis.zrangebyscore(
            self.queue_key,
            min="-inf",
            max=now_ts,
            start=0,
            num=10,
        )

        executed_count = 0
        for item in due_items:
            item_str = item.decode("utf-8") if isinstance(item, bytes) else str(item)
            # Atomically claim action by removing from ZSET
            removed = await self.redis.zrem(self.queue_key, item_str)
            if not removed:
                continue

            try:
                action_id = UUID(item_str)
                await self.execute_action(action_id)
                executed_count += 1
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed processing queued action", item=item_str, error=str(exc)
                )

        return executed_count

    async def run(self) -> None:
        """Run send worker polling loop until stopped."""
        logger.info("SendWorker started", queue=self.queue_key)
        await self.recover_pending_actions()

        while not self._stop_event.is_set():
            try:
                executed = await self.poll_due_actions()
                if executed == 0:
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("Error in send worker loop", error=str(exc))
                await asyncio.sleep(1.0)

        logger.info("SendWorker shut down cleanly")

    def stop(self) -> None:
        """Signal worker to terminate gracefully."""
        self._stop_event.set()


async def run_send_worker() -> None:
    """Entrypoint for standalone send worker execution."""
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)

    engine = create_engine(settings.DATABASE_URL)
    session_factory = create_session_factory(engine)
    redis_client = get_redis_client(settings.REDIS_URL)

    worker = SendWorker(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis_client,
    )

    loop = asyncio.get_running_loop()

    def _sig_handler(*_args: Any) -> None:
        logger.info("Termination signal received; stopping send worker...")
        worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sig_handler)
        except NotImplementedError:
            pass

    try:
        await worker.run()
    finally:
        await redis_client.aclose()
        await engine.dispose()


def main() -> None:
    """Send worker CLI entrypoint."""
    asyncio.run(run_send_worker())


if __name__ == "__main__":
    main()
