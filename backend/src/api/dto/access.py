"""四条业务线共享的可见性授权 HTTP DTO 与纯转换辅助函数。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from domain.access import GrantTargetType, VisibilityGrant, VisibilityGrantInput


class VisibilityGrantRequest(BaseModel):
    """创建或修改资源可见范围时的单条授权请求结构。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    target_type: Literal["department", "role"]
    target_id: str = Field(min_length=1, max_length=64)
    include_subdepartments: bool = True

    def to_domain(self) -> VisibilityGrantInput:
        """转换为不可变领域授权输入。"""
        return VisibilityGrantInput(
            target_type=GrantTargetType(self.target_type),
            target_id=self.target_id,
            include_subdepartments=self.include_subdepartments,
        )


class VisibilityGrantResponse(BaseModel):
    """资源响应中返回的单条可见性授权记录。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    target_type: Literal["department", "role"]
    target_id: str
    include_subdepartments: bool
    created_at: datetime


def grants_to_response(grants: Sequence[VisibilityGrant]) -> list[VisibilityGrantResponse]:
    """将领域授权记录序列转换为 HTTP 响应 DTO 列表。"""
    return [VisibilityGrantResponse.model_validate(grant) for grant in grants]


__all__ = [
    "VisibilityGrantRequest",
    "VisibilityGrantResponse",
    "grants_to_response",
]
