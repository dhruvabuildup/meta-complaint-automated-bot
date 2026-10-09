"""Background queue consumer for processing incoming Meta webhook events."""

import asyncio
import json
import signal
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meta_bot.config import Settings, get_settings
from meta_bot.domain.models import SystemClock
from meta_bot.infra.db.engine import create_engine
from meta_bot.infra.db.repositories import (
    SqlAlchemyActionRepository,
    SqlAlchemyCommentRepository,
    SqlAlchemyContactRepository,
    SqlAlchemyRawEventRepository,
)
from meta_bot.infra.db.session import create_session_factory
from meta_bot.infra.redis.client import get_redis_client
from meta_bot.infra.redis.dedupe import RedisDedupeAdapter
from meta_bot.infra.redis.keys import (
    dead_letter_queue_key,
    event_processing_queue_key,
    event_queue_key,
)
from meta_bot.logging_setup import setup_logging
from meta_bot.services.pipeline import EventPipeline

logger = structlog.get_logger(__name__)


class IngestWorker:
    """Consumes incoming webhook events reliably using two-phase Redis queues."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: Redis,
        pipeline: EventPipeline | None = None,
        max_attempts: int = 3,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.redis = redis_client
        self.queue_key = event_queue_key()
        self.processing_key = event_processing_queue_key()
        self.dlq_key = dead_letter_queue_key()
        self.max_attempts = max_attempts
        self._stop_event = asyncio.Event()
        self.pipeline: EventPipeline | None = pipeline

        if pipeline is None:
            # Wire default pipeline components
            self._dedupe_adapter = RedisDedupeAdapter(redis_client)
            self._clock = SystemClock()

    async def get_dlq_count(self) -> int:
        """Return the number of failed events currently in the dead-letter queue."""
        return int(await self.redis.llen(self.dlq_key))

    async def recover_orphaned_events(self) -> int:
        """Requeue items left in the processing list from a previous crashed run."""
        requeued_count = 0
        while True:
            # Atomically move from processing back to main queue
            try:
                moved = await self.redis.lmove(
                    self.processing_key,
                    self.queue_key,
                    "LEFT",
                    "RIGHT",
                )
            except (RedisError, AttributeError):
                # Fallback for Redis < 6.2
                moved = await self.redis.rpoplpush(self.processing_key, self.queue_key)

            if not moved:
                break
            requeued_count += 1

        if requeued_count > 0:
            logger.warning(
                "Recovered orphaned events from processing queue", count=requeued_count
            )
        return requeued_count

    async def process_item(self, item_str: str) -> None:
        """Process a single event string with transaction handling and DLQ routing."""
        try:
            data = json.loads(item_str)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error(
                "Malformed JSON in event queue; moving directly to DLQ", error=str(exc)
            )
            await self.redis.rpush(self.dlq_key, item_str)
            await self.redis.lrem(self.processing_key, count=1, value=item_str)
            return

        dedupe_key = data.get("dedupe_key", "unknown")
        attempts = int(data.get("attempts", 0)) + 1

        try:
            async with self.session_factory() as session:
                pipeline = self.pipeline
                if pipeline is None:
                    pipeline = EventPipeline(
                        dedupe_port=self._dedupe_adapter,
                        contact_repo=SqlAlchemyContactRepository(session),
                        comment_repo=SqlAlchemyCommentRepository(session),
                        action_repo=SqlAlchemyActionRepository(session),
                        clock=self._clock,
                        settings=self.settings,
                    )

                # Execute pipeline filters
                decision = await pipeline.process_event(data)

                # Update database raw event record
                raw_repo = SqlAlchemyRawEventRepository(session)
                outcome_status = (
                    decision.kind.value
                    if decision.drop_reason is None
                    else f"DROPPED_{decision.drop_reason.value}"
                )
                await raw_repo.mark_processed(
                    dedupe_key=dedupe_key, status=outcome_status
                )
                await session.commit()

            # Acknowledge completion: remove from in-flight processing list
            await self.redis.lrem(self.processing_key, count=1, value=item_str)
            logger.info(
                "Successfully processed queue item",
                dedupe_key=dedupe_key,
                decision=outcome_status,
            )

        except Exception as exc:  # noqa: BLE001
            # Top-level worker task handler: prevent loop termination and manage retry / DLQ
            logger.error(
                "Error processing event from queue",
                dedupe_key=dedupe_key,
                attempt=attempts,
                error=str(exc),
            )
            # Remove from processing list
            await self.redis.lrem(self.processing_key, count=1, value=item_str)

            if attempts < self.max_attempts:
                # Retry with updated attempt counter
                data["attempts"] = attempts
                await self.redis.rpush(self.queue_key, json.dumps(data))
                logger.info(
                    "Re-enqueued failed event for retry",
                    dedupe_key=dedupe_key,
                    attempt=attempts,
                )
            else:
                # Poison event: move to Dead Letter Queue
                logger.critical(
                    "Poison event exceeded max attempts; moved to DLQ",
                    dedupe_key=dedupe_key,
                )
                await self.redis.rpush(self.dlq_key, item_str)
                try:
                    async with self.session_factory() as session:
                        raw_repo = SqlAlchemyRawEventRepository(session)
                        await raw_repo.mark_processed(
                            dedupe_key=dedupe_key, status="DEAD_LETTER"
                        )
                        await session.commit()
                except Exception as audit_exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to record DEAD_LETTER status in DB",
                        error=str(audit_exc),
                    )

    async def run(self) -> None:
        """Run consumer loop until stop event is signaled."""
        logger.info("IngestWorker queue consumer started", queue=self.queue_key)
        await self.recover_orphaned_events()

        while not self._stop_event.is_set():
            try:
                # Atomically move next item from queue to processing list
                try:
                    item_res = await self.redis.lmove(
                        self.queue_key,
                        self.processing_key,
                        "LEFT",
                        "RIGHT",
                    )
                except (RedisError, AttributeError):
                    item_res = await self.redis.rpoplpush(
                        self.queue_key, self.processing_key
                    )

                if item_res is None:
                    # Queue is empty, pause briefly
                    await asyncio.sleep(0.2)
                    continue

                item_str = (
                    item_res.decode("utf-8")
                    if isinstance(item_res, bytes)
                    else str(item_res)
                )
                await self.process_item(item_str)

            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                # Worker loop must not crash on unexpected failures
                logger.error("Unexpected error in worker loop", error=str(exc))
                await asyncio.sleep(1.0)

        logger.info("IngestWorker shut down cleanly")

    def stop(self) -> None:
        """Signal worker to stop processing."""
        self._stop_event.set()


async def run_worker() -> None:
    """Entrypoint for standalone worker execution."""
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)

    engine = create_engine(settings.DATABASE_URL)
    session_factory = create_session_factory(engine)
    redis_client = get_redis_client(settings.REDIS_URL)

    worker = IngestWorker(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis_client,
    )

    loop = asyncio.get_running_loop()

    def _sig_handler(*_args: Any) -> None:
        logger.info("Termination signal received; stopping ingest worker...")
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
    """Worker CLI entrypoint."""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
