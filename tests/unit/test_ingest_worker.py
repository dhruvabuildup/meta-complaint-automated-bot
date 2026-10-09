"""Unit tests for background IngestWorker queue consumer."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from meta_bot.config import Settings
from meta_bot.domain.enums import DecisionKind, DropReason
from meta_bot.domain.models import PipelineDecision
from meta_bot.infra.redis.keys import (
    dead_letter_queue_key,
    event_processing_queue_key,
    event_queue_key,
)
from meta_bot.services.pipeline import EventPipeline
from meta_bot.workers.ingest_worker import IngestWorker


@pytest.mark.asyncio
async def test_recover_orphaned_events(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Worker should recover leftover items from processing list back to main queue on boot."""
    processing_key = event_processing_queue_key()
    queue_key = event_queue_key()

    # Seed 3 items in the processing queue (from a previous crashed run)
    await fake_redis.rpush(processing_key, "item_1", "item_2", "item_3")

    session_factory = MagicMock()
    worker = IngestWorker(
        settings=test_settings,
        session_factory=session_factory,
        redis_client=fake_redis,
    )

    recovered = await worker.recover_orphaned_events()
    assert recovered == 3

    # Processing queue must now be empty
    assert await fake_redis.llen(processing_key) == 0
    # Main queue must now contain all 3 recovered items
    assert await fake_redis.llen(queue_key) == 3


@pytest.mark.asyncio
async def test_process_item_success(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Successfully processed item updates DB status and removes from processing queue."""
    processing_key = event_processing_queue_key()
    item_payload = {
        "dedupe_key": "instagram:comments:179001",
        "field": "comments",
        "value": {"id": "179001", "text": "hello"},
    }
    item_str = json.dumps(item_payload)

    # Put item in processing list
    await fake_redis.rpush(processing_key, item_str)

    # Mock pipeline returning PROCEED
    mock_pipeline = AsyncMock(spec=EventPipeline)
    mock_pipeline.process_event.return_value = PipelineDecision(
        kind=DecisionKind.PROCEED
    )

    # Mock session factory and raw repo
    mock_session = AsyncMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session

    worker = IngestWorker(
        settings=test_settings,
        session_factory=mock_session_factory,
        redis_client=fake_redis,
        pipeline=mock_pipeline,
    )

    await worker.process_item(item_str)

    # Verify pipeline was called
    mock_pipeline.process_event.assert_called_once_with(item_payload)

    # Verify session execute (update status to PROCEED) and commit were called
    assert mock_session.execute.called
    assert mock_session.commit.called

    # Item should be removed from processing queue
    assert await fake_redis.llen(processing_key) == 0


@pytest.mark.asyncio
async def test_process_item_dropped(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Dropped item records DROPPED_<reason> status in DB."""
    processing_key = event_processing_queue_key()
    item_payload = {"dedupe_key": "instagram:comments:spam1"}
    item_str = json.dumps(item_payload)
    await fake_redis.rpush(processing_key, item_str)

    mock_pipeline = AsyncMock(spec=EventPipeline)
    mock_pipeline.process_event.return_value = PipelineDecision(
        kind=DecisionKind.DROP,
        drop_reason=DropReason.SPAM_OR_PAGE,
    )

    mock_session = AsyncMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session

    worker = IngestWorker(
        settings=test_settings,
        session_factory=mock_session_factory,
        redis_client=fake_redis,
        pipeline=mock_pipeline,
    )

    await worker.process_item(item_str)
    assert await fake_redis.llen(processing_key) == 0


@pytest.mark.asyncio
async def test_process_malformed_json_moves_to_dlq(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Malformed non-JSON item in queue is routed immediately to DLQ."""
    processing_key = event_processing_queue_key()
    dlq_key = dead_letter_queue_key()

    bad_item = "NOT_A_JSON_STRING"
    await fake_redis.rpush(processing_key, bad_item)

    worker = IngestWorker(
        settings=test_settings,
        session_factory=MagicMock(),
        redis_client=fake_redis,
    )

    await worker.process_item(bad_item)

    # Removed from processing queue
    assert await fake_redis.llen(processing_key) == 0
    # Added to dead letter queue
    assert await fake_redis.llen(dlq_key) == 1
    assert await worker.get_dlq_count() == 1


@pytest.mark.asyncio
async def test_retry_transient_failure_then_dlq(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Transient failures are retried up to max_attempts before moving to DLQ."""
    processing_key = event_processing_queue_key()
    queue_key = event_queue_key()
    dlq_key = dead_letter_queue_key()

    item_payload = {"dedupe_key": "fail_key_101"}
    item_str = json.dumps(item_payload)
    await fake_redis.rpush(processing_key, item_str)

    mock_pipeline = AsyncMock(spec=EventPipeline)
    mock_pipeline.process_event.side_effect = RuntimeError("Transient DB glitch")

    mock_session = AsyncMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session

    worker = IngestWorker(
        settings=test_settings,
        session_factory=mock_session_factory,
        redis_client=fake_redis,
        pipeline=mock_pipeline,
        max_attempts=2,
    )

    # Attempt 1: should re-enqueue to main queue with attempts=1
    await worker.process_item(item_str)
    assert await fake_redis.llen(processing_key) == 0
    assert await fake_redis.llen(queue_key) == 1
    assert await fake_redis.llen(dlq_key) == 0

    re_enqueued = await fake_redis.lpop(queue_key)
    assert isinstance(re_enqueued, str)
    data = json.loads(re_enqueued)
    assert data["attempts"] == 1

    # Attempt 2 (reaches max_attempts=2): should move to DLQ
    await fake_redis.rpush(processing_key, re_enqueued)
    await worker.process_item(re_enqueued)
    assert await fake_redis.llen(processing_key) == 0
    assert await fake_redis.llen(queue_key) == 0
    assert await fake_redis.llen(dlq_key) == 1


@pytest.mark.asyncio
async def test_worker_run_and_graceful_stop(
    test_settings: Settings,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Worker run loop stops cleanly when stop() is called."""
    mock_session_factory = MagicMock()
    worker = IngestWorker(
        settings=test_settings,
        session_factory=mock_session_factory,
        redis_client=fake_redis,
    )

    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.05)
    worker.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
