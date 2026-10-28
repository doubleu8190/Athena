"""Worker 执行租约的领域身份和值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

_WorkPayload = TypeVar("_WorkPayload")


@dataclass(frozen=True, slots=True)
class LeaseKey:
    """稳定标识一个需要独占执行权的资源。"""

    resource_type: str
    resource_id: str

    def __post_init__(self) -> None:
        """拒绝不完整的租约资源身份。"""
        if not self.resource_type.strip() or not self.resource_id.strip():
            raise ValueError("lease resource type and ID are required")


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    """表示一次带单调 fencing token 的租约授权。"""

    key: LeaseKey
    owner_id: str
    fencing_token: int
    lease_until: datetime

    def __post_init__(self) -> None:
        """校验授权身份和 token。"""
        if not self.owner_id.strip():
            raise ValueError("lease owner ID is required")
        if self.fencing_token < 1:
            raise ValueError("lease fencing token must be positive")


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    """租约中心向调用方公开的资源状态。"""

    key: LeaseKey
    state: str
    owner_id: str | None
    fencing_token: int | None
    lease_until: datetime | None


@dataclass(frozen=True, slots=True)
class ClaimedWork(Generic[_WorkPayload]):
    """把队列中的业务内容与对应的执行授权绑定。"""

    payload: _WorkPayload
    lease: LeaseGrant


class LeaseLostError(RuntimeError):
    """当前执行已失去租约，不能再提交正常结果。"""
