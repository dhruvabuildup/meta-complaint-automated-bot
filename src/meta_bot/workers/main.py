"""Background worker process entrypoint."""

import asyncio
import signal
from typing import Any

import structlog

from meta_bot.config import get_settings
from meta_bot.logging_setup import setup_logging

logger = structlog.get_logger(__name__)


async def run_worker() -> None:
    """Run worker event loop until cancellation or termination signal."""
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)

    logger.info("MetaBot background worker starting", env=settings.APP_ENV)

    stop_event = asyncio.Event()

    def _signal_handler(*_args: Any) -> None:
        logger.info("Shutdown signal received in worker, stopping...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    # Placeholder worker loop (queues and processing added in subsequent phases)
    while not stop_event.is_set():
        try:
            logger.debug("Worker heartbeat tick")
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break

    logger.info("MetaBot background worker shutdown cleanly")


def main() -> None:
    """Entry point for worker CLI."""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
