"""Celery app configuration and task definitions.

Redis broker: DB 1 (task queue)
Redis result backend: DB 2
Tasks are JSON-serialized.
"""

from __future__ import annotations

from celery import Celery

from athena.config import get_config

config = get_config()

celery_app = Celery(
    "athena",
    broker=config.celery_broker_url,
    backend=config.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    worker_concurrency=8,
    broker_connection_retry_on_startup=True,
)

# Auto-discover tasks
celery_app.autodiscover_tasks([
    "athena.tasks.memory_sync",
    "athena.tasks.cleanup",
    "athena.tasks.conversation_extract",
])
