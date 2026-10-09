"""Background worker process entrypoint."""

from meta_bot.workers.ingest_worker import main

if __name__ == "__main__":
    main()
