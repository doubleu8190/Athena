"""Worker 跨进程资源和执行授权的纯领域对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypeAlias

from .worker_entities import JsonValue

ResourceInput: TypeAlias = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class WorkerExecutionResourceScope:
    """绑定当前执行身份及其可访问的资源集合。"""

    service_principal: str
    worker_instance_id: str
    worker_instance_key: str
    run_id: str
    plan_id: str
    task_id: str
    execution_id: str
    generation: int
    cancellation_epoch: int
    read_resource_ids: tuple[str, ...]
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class WorkerExecutionResourceContext:
    """提供签发授权所需的活动执行范围和冻结输入。"""

    scope: WorkerExecutionResourceScope
    task_input: ResourceInput


@dataclass(frozen=True, slots=True)
class WorkerResourceReference:
    """Worker 上传资源后可放入执行结果的协议引用。"""

    resource_id: str
    media_type: str
    size_bytes: int
    sha256: str
    file_name: str | None


__all__ = [
    "ResourceInput",
    "WorkerExecutionResourceContext",
    "WorkerExecutionResourceScope",
    "WorkerResourceReference",
]
