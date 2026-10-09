"""Background worker process entrypoint running ingest and send consumers concurrently."""

import asyncio
import signal
from typing import Any

import structlog

from meta_bot.config import get_settings
from meta_bot.infra.db.engine import create_engine
from meta_bot.infra.db.session import create_session_factory
from meta_bot.infra.redis.client import get_redis_client
from meta_bot.logging_setup import setup_logging
from meta_bot.workers.ingest_worker import IngestWorker
from meta_bot.workers.send_worker import SendWorker

logger = structlog.get_logger(__name__)


async def run_all() -> None:
    """Run both the ingest queue worker and the paced send worker concurrently."""
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)

    engine = create_engine(settings.DATABASE_URL)
    session_factory = create_session_factory(engine)
    redis_client = get_redis_client(settings.REDIS_URL)

    ingest_worker = IngestWorker(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis_client,
    )
    send_worker = SendWorker(
        settings=settings,
        session_factory=session_factory,
        redis_client=redis_client,
    )

    loop = asyncio.get_running_loop()

    def _sig_handler(*_args: Any) -> None:
        logger.info("Signal received, stopping workers...")
        ingest_worker.stop()
        send_worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sig_handler)
        except NotImplementedError:
            pass

    try:
        await asyncio.gather(
            ingest_worker.run(),
            send_worker.run(),
        )
    finally:
        await redis_client.aclose()
        await engine.dispose()


def main() -> None:
    """CLI worker entrypoint."""
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
