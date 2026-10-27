"""Worker graph execution context 中的租约 grant 转换。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from domain.leases import LeaseGrant, LeaseKey


def lease_grant_from_context(execution: Mapping[str, Any]) -> LeaseGrant:
    """从图状态恢复 Worker execution 绑定的不可变租约身份。"""
    raw = execution.get("lease")
    if not isinstance(raw, Mapping):
        raise ValueError("worker_execution_lease_missing")
    key = raw.get("key")
    if not isinstance(key, Mapping):
        raise ValueError("worker_execution_lease_key_missing")
    expires = raw.get("lease_until")
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires)
    if not isinstance(expires, datetime):
        raise ValueError("worker_execution_lease_expiry_missing")
    return LeaseGrant(
        key=LeaseKey(
            resource_type=str(key.get("resource_type", "")),
            resource_id=str(key.get("resource_id", "")),
        ),
        owner_id=str(raw.get("owner_id", "")),
        fencing_token=int(raw.get("fencing_token", 0)),
        lease_until=expires,
    )


__all__ = ["lease_grant_from_context"]
