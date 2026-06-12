"""Celery worker entry point.

Run as: celery -A athena.worker worker --concurrency=8
"""

from __future__ import annotations

from athena.celery_app import celery_app


def main():
    """Entry point for Celery worker."""
    celery_app.start()


if __name__ == "__main__":
    main()
