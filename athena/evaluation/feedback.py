"""记录经过明确授权的反馈，不将其写入用户消息。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from athena.evaluation.redaction import redact_request
from athena.evaluation.storage import JsonlEventStore
from athena.utils.ids import generate_time_id


@dataclass(frozen=True)
class FeedbackTask:
    """表示一个脱离用户消息保存的反馈任务。"""

    task_id: str
    event_id: str
    query_hash: str
    scope: dict[str, Any]
    candidate_ids: tuple[str, ...]
    created_at: str
    status: str = "open"


class FeedbackService:
    """创建仅保存脱敏标识的人工反馈任务。"""

    def __init__(self, store: JsonlEventStore, *, hash_salt: str = "feedback") -> None:
        """创建反馈服务。

        参数：
            store: 反馈事件存储。
            hash_salt: 查询哈希盐；应使用部署级稳定值。
        """
        self.store = store
        self.hash_salt = hash_salt

    def create_task(
        self,
        *,
        event_id: str,
        query: str,
        scope: dict[str, Any],
        candidate_ids: list[str],
    ) -> FeedbackTask:
        """创建反馈任务并仅持久化脱敏后的查询标识。

        参数：
            event_id: 触发反馈的检索事件 ID。
            query: 原始查询，仅用于计算哈希，不会写入事件。
            scope: 检索范围元数据。
            candidate_ids: 候选结果 ID 列表。

        返回值：
            新建的 ``FeedbackTask``。

        异常：
            ValueError: 查询或范围包含不允许持久化的敏感文本。
        """
        request = redact_request(query, scope, salt=self.hash_salt)
        task = FeedbackTask(
            generate_time_id(),
            event_id,
            request.query_hash,
            dict(scope),
            tuple(candidate_ids),
            datetime.now(timezone.utc).isoformat(),
        )
        self.store.append({"type": "feedback_task", **task.__dict__})
        return task
