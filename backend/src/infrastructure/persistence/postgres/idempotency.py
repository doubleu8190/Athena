"""HTTP 写操作共用的事务型幂等回执。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import IdempotencyReceiptModel

T = TypeVar("T")


class IdempotencyConflictError(ValueError):
    """同一请求 ID 被用于不同参数时抛出的业务冲突。"""


async def execute_idempotent(
    session: AsyncSession,
    *,
    scope: str,
    request_id: str,
    parameters: object,
    action: Callable[[], Awaitable[T]],
    encode: Callable[[T], dict[str, Any]],
    decode: Callable[[dict[str, Any]], T],
) -> tuple[T, bool]:
    """在当前业务事务中执行幂等操作并保存首次成功回执。

    参数：session 是当前事务；scope 和 request_id 定义幂等作用域；
    parameters 用于检测请求冲突；action 执行业务写入；encode/decode 转换回执。
    返回：业务结果及是否重放已有回执。
    异常：参数不一致时抛出 IdempotencyConflictError；业务异常由 action 原样抛出。
    """
    if not scope.strip() or not request_id.strip():
        raise ValueError("idempotency_key_required")
    canonical = json.dumps(
        parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    parameter_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    lock_key = f"idempotency:{scope}:{request_id}"
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
    )
    existing = await session.get(
        IdempotencyReceiptModel, (scope, request_id)
    )
    if existing is not None:
        if existing.parameter_hash != parameter_hash:
            raise IdempotencyConflictError("idempotency_conflict")
        return decode(existing.response_json), True

    value = await action()
    session.add(
        IdempotencyReceiptModel(
            scope=scope,
            request_id=request_id,
            parameter_hash=parameter_hash,
            response_json=encode(value),
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.flush()
    return value, False
