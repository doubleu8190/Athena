"""Worker 健康探测与可用实例查询的领域记录。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypeAlias

from .worker_entities import JsonValue


class WorkerInstancePrincipalMismatch(PermissionError):
    """实例键已由其他认证主体持有，不能隐式转移所有权。"""


@dataclass(frozen=True, slots=True)
class WorkerHealthProbeTarget:
    """描述一次健康探测所使用的不可变 Worker 注册快照。"""

    instance_id: str
    instance_key: str
    service_principal: str
    base_url: str
    registration_updated_at: datetime


@dataclass(frozen=True, slots=True)
class WorkerHealthProbeResult:
    """描述已按 v1 HealthResponse 校验的远程健康响应。"""

    instance_key: str
    status: str
    accepting_tasks: bool
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class WorkerAvailableInstance:
    """暴露给规划与派发逻辑的能力、超时和容量快照。"""

    instance_id: str
    instance_key: str
    contract_id: str
    contract_revision: int
    execution_timeout_seconds: int
    max_concurrency: int
    current_concurrency: int
    available_concurrency: int
    can_do: tuple[str, ...]
    cannot_do: tuple[str, ...]
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class WorkerHealthTransition:
    """记录一次 CAS 成功的健康探测及接单状态转换。"""

    instance_id: str
    status: str
    accepting_tasks: bool
    failure_count: int
    success_count: int
    updated_at: datetime


__all__ = [
    "WorkerInstancePrincipalMismatch",
    "WorkerAvailableInstance",
    "WorkerHealthProbeResult",
    "WorkerHealthProbeTarget",
    "WorkerHealthTransition",
]
