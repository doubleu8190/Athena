"""Tests for the shared structured logging facade."""

from __future__ import annotations

import json

from shared.logging import get_logger


def test_get_logger_emits_structured_context(caplog) -> None:
    caplog.set_level("INFO")
    logger = get_logger("athena.tests.structured")

    logger.info("test_event", run_id="run-123")

    record = next(
        record for record in caplog.records if record.name == "athena.tests.structured"
    )
    entry = json.loads(record.getMessage())
    assert entry["event"] == "test_event"
    assert entry["run_id"] == "run-123"


def test_exception_log_retains_traceback(caplog) -> None:
    caplog.set_level("ERROR")
    logger = get_logger("athena.tests.structured.exception")

    try:
        raise RuntimeError("expected failure")
    except RuntimeError as error:
        logger.error("test_failure", job_id="job-123", exc_info=error)

    record = next(
        record
        for record in caplog.records
        if record.name == "athena.tests.structured.exception"
    )
    entry = json.loads(record.getMessage())
    assert entry["event"] == "test_failure"
    assert entry["job_id"] == "job-123"
    assert "RuntimeError: expected failure" in entry["exception"]
