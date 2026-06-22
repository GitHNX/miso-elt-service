"""
Ingestion worker entrypoint.

Usage
─────
  # One-shot (Lambda / ECS run-to-completion):
  python -m src.ingestion.worker --once

  # Continuous daemon (ECS long-running task, polls every 60 s):
  python -m src.ingestion.worker

The --once flag makes it suitable for EventBridge-scheduled ECS tasks:
EventBridge triggers the task, it runs once, then the container exits.
The alternative (daemon mode) suits a single always-on ECS task.
"""
import argparse
import signal
import sys
import time

from src.core.config import get_settings
from src.core.logging import configure_logging, get_logger
from src.ingestion.service import IngestionService

configure_logging()
logger = get_logger(__name__)

_shutdown = False


def _handle_sigterm(sig, frame):
    global _shutdown
    logger.info("shutdown_signal_received")
    _shutdown = True


def main(once: bool = False) -> None:
    settings = get_settings()
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    service = IngestionService()

    logger.info("worker_starting", mode="once" if once else "daemon", env=settings.environment)

    if once:
        service.run()
        sys.exit(0)

    # Daemon mode — run forever, sleeping between polls
    poll_interval = settings.miso_poll_interval_seconds
    while not _shutdown:
        try:
            service.run()
        except Exception:
            # Errors are logged and metrics published inside service.run()
            pass

        if _shutdown:
            break

        logger.info("worker_sleeping", seconds=poll_interval)
        time.sleep(poll_interval)

    logger.info("worker_stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MISO FuelMix ingestion worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one ingestion cycle then exit (for scheduled ECS tasks)",
    )
    args = parser.parse_args()
    main(once=args.once)
