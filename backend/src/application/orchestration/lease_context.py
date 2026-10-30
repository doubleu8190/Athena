"""将 Worker 图执行上下文中的租约授权转换为领域身份。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from domain.leases import LeaseGrant, LeaseKey


def lease_grant_from_context(execution: Mapping[str, Any]) -> LeaseGrant:
    """从 Worker 执行上下文恢复经过校验的租约授权。

    参数：
    - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。

    返回：
    - LeaseGrant：从执行上下文恢复的租约授权；缺少必要身份或值无效时触发校验异常。

    异常：
    - ValueError：校验条件 `not isinstance(raw, Mapping)` 成立时（异常说明：worker_execution_lease_missing）。
    - ValueError：校验条件 `not isinstance(key, Mapping)` 成立时（异常说明：worker_execution_lease_key_missing）。
    - ValueError：校验条件 `not isinstance(expires, datetime)` 成立时（异常说明：worker_execution_lease_expiry_missing）。
    - 未列出的依赖异常按当前实现传播。
    """
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
