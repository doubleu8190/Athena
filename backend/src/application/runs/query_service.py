"""运行列表查询用例。"""

from __future__ import annotations

from domain.events import EventPort
from domain.runs import RunQueryPort, RunStatus, RunSummary
from domain.sessions import SessionRepository

from application.common.responses import RunListResponse, RunSummaryResponse, RunTimelineEventResponse, RunTimelineResponse

from application.sessions import SessionNotFoundError


class RunNotFoundError(LookupError):
    """查询指定 Run 时未找到对应记录而抛出的异常。
    """


class RunQueryService:
    """把会话存在性校验和运行查询组合起来。"""

    def __init__(
        self,
        sessions: SessionRepository,
        runs: RunQueryPort,
        events: EventPort | None = None,
    ) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `sessions`（SessionRepository）：读取会话身份和状态的仓储依赖；约束：无额外运行时限制。
        - `runs`（RunQueryPort）：本次查询或比较涉及的运行记录；约束：无额外运行时限制。
        - `events`（EventPort | None）：用于读取、写入或推送的事件存储依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._sessions = sessions
        self._runs = runs
        self._events = events

    async def list_for_session(self, session_id: str) -> list[RunSummaryResponse]:
        """列出指定会话的 Run 摘要。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - list[RunSummaryResponse]：符合条件的有序结果集合；集合成员。

        异常：
        - SessionNotFoundError：校验条件 `await self._sessions.get(session_id) is None` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)
        return [_to_response(value) for value in await self._runs.list_for_session(session_id)]

    async def list_recent(
        self, *, limit: int = 50, offset: int = 0, session_id: str | None = None,
    ) -> RunListResponse:
        """分页查询运行历史，可按会话筛选。

        参数：
        - `limit`（int）：每页条数，范围为 1 到 200；约束：可省略，默认值为 `50`。
        - `offset`（int）：已跳过的记录数，负数按零处理；约束：可省略，默认值为 `0`。
        - `session_id`（str | None）：可选会话 ID；为空时查询全部未删除会话；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`；须通过方法内校验；失败条件见下方异常说明。

        返回：
        - RunListResponse：运行摘要、总条数及是否存在下一页。

        异常：
        - SessionNotFoundError：校验条件 `session_id is not None and await self._sessions.get(session_id) is None` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        if session_id is not None and await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)
        offset = max(0, offset)
        values, total = await self._runs.list_recent(
            limit=min(max(limit, 1), 200), offset=offset, session_id=session_id,
        )
        return RunListResponse(
            items=tuple(_to_response(value) for value in values),
            total=total,
            has_more=offset + len(values) < total,
        )

    async def get(self, run_id: str) -> RunSummaryResponse:
        """按稳定标识读取 Run 摘要；不存在时抛出未找到异常。

        参数：
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。

        返回：
        - RunSummaryResponse：Run 标识、状态、摘要和时间信息组成的响应。

        异常：
        - RunNotFoundError：校验条件 `value is None` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        value = await self._runs.get(run_id)
        if value is None:
            raise RunNotFoundError(run_id)
        return _to_response(value)

    async def timeline(self, session_id: str, run_id: str, *, after: int = 0, limit: int = 200) -> RunTimelineResponse:
        """校验会话和 Run 身份后，读取分页事件及关联资源标识。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：须通过方法内校验；失败条件见下方异常说明。
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `after`（int）：分页或事件回放的起始位置；具体边界语义由对应查询定义；约束：可省略，默认值为 `0`。
        - `limit`（int）：本次读取或处理的数量上限；约束：可省略，默认值为 `200`。

        返回：
        - RunTimelineResponse：会话 Run 摘要及对应分页事件集合。

        异常：
        - SessionNotFoundError：校验条件 `await self._sessions.get(session_id) is None` 成立时。
        - RunNotFoundError：校验条件 `run is None` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)
        run = next((item for item in await self._runs.list_for_session(session_id) if item.run_id == run_id), None)
        if run is None:
            raise RunNotFoundError(run_id)
        cursor = max(0, after)
        if self._events is None:
            return RunTimelineResponse(run=_to_timeline_run_response(run), events=(), next_after_seq=cursor, has_more=False)
        values, has_more = await self._events.list_events_for_run(
            session_id, run_id, after=cursor, limit=min(max(limit, 1), 500)
        )
        events = tuple(_timeline_event(value) for value in values if value.session_seq is not None)
        next_after = events[-1].session_seq if events else cursor
        retrieval_ids = tuple(dict.fromkeys(
            str(event.payload["retrieval_run_id"])
            for event in events
            if event.payload.get("retrieval_run_id")
        ))
        plan_ids = tuple(dict.fromkeys(str(event.payload["plan_id"]) for event in events if event.payload.get("plan_id")))
        task_ids = tuple(dict.fromkeys(str(event.payload["task_id"]) for event in events if event.payload.get("task_id")))
        return RunTimelineResponse(
            run=_to_timeline_run_response(run), events=events, next_after_seq=next_after, has_more=has_more,
            links={"retrieval_run_ids": retrieval_ids, "plan_ids": plan_ids, "task_ids": task_ids},
        )


def _to_response(value: RunSummary) -> RunSummaryResponse:
    """将运行摘要实体转换为应用层响应。

    参数：
    - `value`（RunSummary）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - RunSummaryResponse：Run 标识、状态、摘要和时间信息组成的响应。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return RunSummaryResponse(
        run_id=value.run_id,
        session_id=value.session_id,
        message_id=value.message_id,
        sequence=value.sequence,
        status=value.status.value if isinstance(value.status, RunStatus) else str(value.status),
        root_thread_id=value.root_thread_id,
        error=value.error,
        created_at=value.created_at,
        updated_at=value.updated_at,
        started_at=value.started_at,
        title=value.title,
    )


def _to_timeline_run_response(value: RunSummary) -> RunSummaryResponse:
    """将 Run 摘要转换为时间线响应。

    参数：
    - `value`（RunSummary）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - RunSummaryResponse：Run 标识、状态、摘要和时间信息组成的响应。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    response = _to_response(value)
    error = response.error
    if not isinstance(error, dict):
        safe_error = None
    else:
        safe_error = {
            key: _safe_text(item) if key == "message" else item
            for key, item in error.items()
            if key in {"code", "message", "retryable", "stage"}
        }
    return RunSummaryResponse(
        run_id=response.run_id,
        session_id=response.session_id,
        message_id=response.message_id,
        sequence=response.sequence,
        status=response.status,
        root_thread_id=response.root_thread_id,
        error=safe_error,
        created_at=response.created_at,
        updated_at=response.updated_at,
        started_at=response.started_at,
        title=response.title,
    )


_TIMELINE_FIELDS = {
    "activity_id", "activity_type", "name", "node", "status", "summary", "duration_ms",
    "started_at", "completed_at", "parent_activity_id", "parent_run_id", "plan_id", "task_id",
    "execution_generation", "worker_run_id", "tool_call_id", "call_id", "retrieval_run_id",
    "provider", "model", "attempt", "input_tokens", "output_tokens", "raw_hit_count", "candidate_count", "result_count", "selected_count", "bundle_count", "context_count", "selection_status",
    "decision", "tool_name", "title", "stage", "retryable", "code", "error", "trace_status",
    "approval_id", "risk_level",
}


def _safe_text(value: object, limit: int = 240) -> str:
    """将任意输入整理为长度受限的单行文本。

    参数：
    - `value`（object）：待解析、校验或转换的输入值；约束：无额外运行时限制。
    - `limit`（int）：本次读取或处理的数量上限；约束：可省略，默认值为 `240`。

    返回：
    - str：去除控制字符并限制长度后的单行展示文本。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _safe_payload(payload: dict[str, object]) -> dict[str, object]:
    """筛除不适合对外展示的载荷字段并规范化值。

    参数：
    - `payload`（dict[str, object]）：随事件或消息传递的结构化内容；约束：无额外运行时限制。

    返回：
    - dict[str, object]：移除敏感或非公开字段并规范化后的事件载荷映射。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    safe: dict[str, object] = {}
    for key, value in payload.items():
        if key not in _TIMELINE_FIELDS:
            continue
        if key == "error" and isinstance(value, dict):
            safe["error"] = {
                nested_key: (_safe_text(nested_value) if nested_key == "message" else nested_value)
                for nested_key, nested_value in value.items()
                if nested_key in {"code", "message", "retryable", "stage"}
            }
        elif isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = _safe_text(value) if isinstance(value, str) else value
    if "error" not in safe and isinstance(payload.get("error_detail"), dict):
        error = payload["error_detail"]
        safe["error"] = {
            nested_key: (_safe_text(nested_value) if nested_key == "message" else nested_value)
            for nested_key, nested_value in error.items()
            if nested_key in {"code", "message", "retryable", "stage"}
        }
    return safe


def _timeline_event(value: object) -> RunTimelineEventResponse:
    """将事件对象转换为时间线事件响应。

    参数：
    - `value`（object）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - RunTimelineEventResponse：供调用方展示或继续处理的 `RunTimelineEventResponse` 响应对象。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    event = value
    return RunTimelineEventResponse(
        session_seq=int(event.session_seq),
        event_type=str(event.event_type),
        occurred_at=event.occurred_at,
        run_id=event.run_id,
        payload=_safe_payload(dict(event.payload)),
    )

__all__ = ["RunNotFoundError", "RunQueryService"]
