"""ARQ Worker entry point for Athena background tasks.

Starts the ARQ worker which processes background tasks like
conversation insight extraction.

Usage:
    # Direct script
    python -m athena.worker

    # Or via entry point
    athena-worker

    # Or via arq CLI
    arq athena.arq_worker.WorkerSettings
"""

from __future__ import annotations

import asyncio
import sys

from athena.logging_config import get_logger

logger = get_logger(__name__)


async def _run() -> None:
    """Run the ARQ worker."""
    from arq import run_worker

    from athena.arq_worker import WorkerSettings

    logger.info("arq_worker_starting")
    await run_worker(WorkerSettings)


def main() -> None:
    """Entry point for the ARQ worker."""
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("arq_worker_interrupted")
        sys.exit(0)
    except Exception:
        logger.exception("arq_worker_failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
