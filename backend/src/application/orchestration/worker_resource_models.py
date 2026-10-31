"""Worker 资源 API 的稳定 JSON 模型。"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WorkerResourceReferenceModel(BaseModel):
    """供 Worker 结果引用的资源摘要，不包含本地路径或访问凭证。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"] = "1.0"
    resource_id: UUID
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_name: str | None = Field(default=None, max_length=255)


__all__ = ["WorkerResourceReferenceModel"]
