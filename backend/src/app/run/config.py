"""Run Worker 的有界并发与租约设置。"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass


def _positive_int(name: str, default: int, maximum: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name}_invalid") from error
    if not 1 <= parsed <= maximum:
        raise ValueError(f"{name}_invalid")
    return parsed


@dataclass(frozen=True, slots=True)
class RunWorkerConfig:
    """限制 Worker 并发、轮询与执行租约的部署参数。"""

    concurrency: int = 4
    lease_seconds: int = 30
    heartbeat_seconds: int = 5
    execution_seconds: int = 300
    cancel_poll_seconds: float = 1.0
    queue_poll_seconds: float = 1.0
    reclaim_seconds: float = 5.0
    claim_batch_size: int = 1
    llm_request_limit: int = 40
    tool_timeout_seconds: float = 30.0
    tool_argument_bytes: int = 64 * 1024
    tool_result_bytes: int = 64 * 1024
    tool_cleanup_seconds: float = 10.0
    tool_batch_limit: int = 8
    tool_concurrency: int = 4

    def __post_init__(self) -> None:
        if (
            not 1 <= self.concurrency <= 64
            or not 3 <= self.lease_seconds <= 3600
            or not 1 <= self.heartbeat_seconds < self.lease_seconds
            or not 1 <= self.execution_seconds <= 86_400
            or not all(
                math.isfinite(value) and value > 0
                for value in (
                    self.cancel_poll_seconds,
                    self.queue_poll_seconds,
                    self.reclaim_seconds,
                    self.tool_timeout_seconds,
                    self.tool_cleanup_seconds,
                )
            )
            or self.claim_batch_size != 1
            or not 1 <= self.llm_request_limit <= 40
            or not 0 < self.tool_timeout_seconds <= 300
            or not 0 < self.tool_cleanup_seconds <= 10
            or not 512 <= self.tool_result_bytes <= 64 * 1024
            or not 1024 <= self.tool_argument_bytes <= 64 * 1024
            or not 1 <= self.tool_batch_limit <= 8
            or not 1 <= self.tool_concurrency <= 8
            or self.tool_concurrency > self.tool_batch_limit
        ):
            raise ValueError("run_worker_config_invalid")


def worker_config_from_environment() -> RunWorkerConfig:
    """读取受限 Worker 参数，拒绝无效并发或租约配置。"""
    lease_seconds = _positive_int("ATHENA_RUN_LEASE_SECONDS", 30, 3600)
    heartbeat_seconds = _positive_int("ATHENA_RUN_HEARTBEAT_SECONDS", 5, 3599)
    return RunWorkerConfig(
        concurrency=_positive_int("ATHENA_RUN_WORKER_CONCURRENCY", 4, 64),
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
        execution_seconds=_positive_int("ATHENA_RUN_EXECUTION_SECONDS", 300, 86_400),
        cancel_poll_seconds=float(os.getenv("ATHENA_RUN_CANCEL_POLL_SECONDS", "1")),
        queue_poll_seconds=float(os.getenv("ATHENA_RUN_QUEUE_POLL_SECONDS", "1")),
        reclaim_seconds=float(os.getenv("ATHENA_RUN_RECLAIM_SECONDS", "5")),
        llm_request_limit=_positive_int("ATHENA_RUN_LLM_REQUEST_LIMIT", 40, 40),
        tool_timeout_seconds=_positive_float("ATHENA_RUN_TOOL_TIMEOUT_SECONDS", 30.0, 300.0),
        tool_argument_bytes=_positive_int("ATHENA_RUN_TOOL_ARGUMENT_BYTES", 64 * 1024, 64 * 1024),
        tool_result_bytes=_positive_int("ATHENA_RUN_TOOL_RESULT_BYTES", 64 * 1024, 64 * 1024),
        tool_cleanup_seconds=_positive_float("ATHENA_RUN_TOOL_CLEANUP_SECONDS", 10.0, 10.0),
        tool_batch_limit=_positive_int("ATHENA_RUN_TOOL_BATCH_LIMIT", 8, 8),
        tool_concurrency=_positive_int("ATHENA_RUN_TOOL_CONCURRENCY", 4, 8),
    )


def _positive_float(name: str, default: float, maximum: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{name}_invalid") from error
    if not math.isfinite(parsed) or not 0 < parsed <= maximum:
        raise ValueError(f"{name}_invalid")
    return parsed


__all__ = ["RunWorkerConfig", "worker_config_from_environment"]
