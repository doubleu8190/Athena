"""注册中心管理查询的 HTTP 响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, JsonValue


class WorkerRegistryInstanceResponse(BaseModel):
    """描述管理页展示的实例、健康状态、容量与契约版本。"""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    instance_id: str
    instance_key: str
    service_principal: str
    base_url: str
    status: str
    accepting_tasks: bool
    health_identity_valid: bool
    consecutive_health_failures: int
    consecutive_health_successes: int
    last_health_check_at: datetime | None
    created_at: datetime
    updated_at: datetime
    contract_id: str
    contract_revision: int
    execution_timeout_seconds: int
    max_concurrency: int
    current_concurrency: int
    available_concurrency: int
    can_do: list[str]
    cannot_do: list[str]
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]


__all__ = ["WorkerRegistryInstanceResponse"]
