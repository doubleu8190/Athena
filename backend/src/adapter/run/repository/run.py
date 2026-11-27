"""Run 原子命令的 PostgreSQL 门面仓储（组合模式）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.run.repository._tx_helpers import merge_usage
from adapter.run.repository.context_events import RunContextEventStore
from adapter.run.repository.lifecycle import RunLifecycleStore
from adapter.run.repository.subagents import RunSubagentStore
from adapter.run.repository.tool_calls import RunToolCallStore
from domain.message import ConversationMessage, ExecutionMessage
from domain.run.model import (
    AnswerRunCommand,
    BeginSubagentBatchCommand,
    BeginSubagentBatchResult,
    ContextSummary,
    FailRunCommand,
    FinishRunCommand,
    PendingSubagentApproval,
    PendingSubagentInput,
    RecordRunUsageCommand,
    RequestRunInputCommand,
    Run,
    RunDetails,
    RunEvent,
    RunEventPayload,
    RunInputCall,
    RunInputRequest,
    RunLease,
    RunLeaseState,
    RunStreamSnapshot,
    RunUsage,
    SaveContextSummaryCommand,
    SubagentTaskSummary,
    SubmitRunCommand,
    SubmitRunResult,
)
from domain.run.ports import RunRepository
from domain.tool import (
    AdmitToolCallCommand,
    BeginToolCallBatchCommand,
    BeginToolCallBatchResult,
    DecideToolApprovalCommand,
    FinishToolCallCommand,
    InitializeRunToolsCommand,
    PendingToolApproval,
    RuntimeToolSnapshot,
    RunToolBatch,
    RunToolCallDetails,
    RunToolCallPage,
    RunToolCallRecord,
)


class SqlAlchemyRunRepository(RunRepository):
    """组合生命周期、子智能体、工具批处理与上下文事件 Store 的统一仓储门面。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        message_repository: SqlAlchemyMessageRepository,
    ) -> None:
        """保存共享数据库工厂、消息内部写入端口并装配各职责 Store。"""
        self._session_factory = session_factory
        # 统一使用 _message_repo 命名持有时效消息子仓储
        self._message_repo = message_repository
        self._lifecycle = RunLifecycleStore(session_factory, message_repository)
        self._subagents = RunSubagentStore(session_factory, message_repository)
        self._tool_calls = RunToolCallStore(session_factory, message_repository)
        self._context_events = RunContextEventStore(session_factory)

    @staticmethod
    def _merge_usage(current_json: dict[str, object], incoming: RunUsage) -> RunUsage:
        """兼容既有单元测试直接调用门面上的用量幂等聚合辅助方法。"""
        return merge_usage(current_json, incoming)

    async def submit(
        self, session_id: str, owner_id: str, command: SubmitRunCommand
    ) -> SubmitRunResult:
        """写入触发消息、Run、queued 事件与幂等结果。"""
        return await self._lifecycle.submit(session_id, owner_id, command)

    async def answer(self, session_id: str, owner_id: str, command: AnswerRunCommand) -> Run:
        """保存回答及协议结果并将等待 Run 重新排队。"""
        return await self._lifecycle.answer(session_id, owner_id, command)

    async def claim(
        self, worker_id: str, *, lease_seconds: int, execution_seconds: int, limit: int
    ) -> tuple[RunLease, ...]:
        """按 Session → Run 锁顺序领取有限数量的 queued Run。"""
        return await self._lifecycle.claim(
            worker_id,
            lease_seconds=lease_seconds,
            execution_seconds=execution_seconds,
            limit=limit,
        )

    async def reclaim_expired(self, *, limit: int) -> tuple[Run, ...]:
        """把失租 running/cancelling Run 分别收敛为失败或已取消。"""
        return await self._lifecycle.reclaim_expired(limit=limit)

    async def heartbeat(
        self, run_id: str, worker_id: str, execution_epoch: int, *, lease_seconds: int
    ) -> Run | None:
        """仅在数据库时钟下租约仍有效时续约。"""
        return await self._lifecycle.heartbeat(
            run_id, worker_id, execution_epoch, lease_seconds=lease_seconds
        )

    async def record_usage(self, command: RecordRunUsageCommand) -> Run | None:
        """按执行代次和模型调用序号幂等记录已知 usage。"""
        return await self._context_events.record_usage(command)

    async def initialize_tools(
        self, command: InitializeRunToolsCommand
    ) -> RuntimeToolSnapshot | None:
        """有效租约下固定快照；已固定时只返回原快照。"""
        return await self._lifecycle.initialize_tools(command)

    async def begin_tool_call_batch(
        self, command: BeginToolCallBatchCommand
    ) -> BeginToolCallBatchResult:
        """原子保存助手调用消息、全部调用记录和审批等待点。"""
        return await self._tool_calls.begin_tool_call_batch(command)

    async def get_resumable_tool_batch(self, run_id: str) -> RunToolBatch | None:
        """在任何新模型请求前读取已批准但尚未执行的批次。"""
        return await self._tool_calls.get_resumable_tool_batch(run_id)

    async def admit_tool_call(self, command: AdmitToolCallCommand) -> RunToolCallRecord | None:
        """以当前 Worker 和代次原子取得单调用外部执行准入。"""
        return await self._tool_calls.admit_tool_call(command)

    async def decide_tool_approval(self, command: DecideToolApprovalCommand) -> Run | None:
        """逐调用批准/拒绝，最后一个决定原子重新排队 Run。"""
        return await self._tool_calls.decide_tool_approval(command)

    async def list_pending_approvals(
        self, session_id: str, owner_id: str, run_id: str
    ) -> tuple[PendingToolApproval, ...] | None:
        """读取 owner 可见 Run 的安全待审批调用投影。"""
        return await self._tool_calls.list_pending_approvals(session_id, owner_id, run_id)

    async def finish_tool_call(self, command: FinishToolCallCommand) -> Run | None:
        """有效执行权内保存单次工具调用结果与产物，已终结调用安全拒绝覆盖。"""
        return await self._tool_calls.finish_tool_call(command)

    async def list_tool_calls(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        *,
        after_ordinal: int,
        limit: int,
    ) -> RunToolCallPage | None:
        """按 Run 内调用序号游标读取安全摘要。"""
        return await self._tool_calls.list_tool_calls(
            session_id,
            owner_id,
            run_id,
            after_ordinal=after_ordinal,
            limit=limit,
        )

    async def get_tool_call(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        call_id: str,
    ) -> RunToolCallDetails | None:
        """读取授权调用详情，不暴露内部 Server 或异常。"""
        return await self._tool_calls.get_tool_call(session_id, owner_id, run_id, call_id)

    async def request_input(self, command: RequestRunInputCommand) -> RunInputRequest:
        """原子保存澄清消息、调用轨迹、输入请求与 waiting 状态事件。"""
        return await self._lifecycle.request_input(command)

    async def finish(self, command: FinishRunCommand) -> Run | None:
        """校验执行租约并原子保存最终回复、用量和终态事件。"""
        return await self._lifecycle.finish(command)

    async def fail(self, command: FailRunCommand) -> Run | None:
        """校验执行租约并原子保存安全失败状态和事件。"""
        return await self._lifecycle.fail(command)

    async def settle_cancelled(
        self,
        run_id: str,
        worker_id: str,
        execution_epoch: int,
        settled_at: datetime,
        *,
        llm_request_count: int | None = None,
    ) -> Run | None:
        """在取消清理完成后把当前 cancelling Run 原子收敛为 cancelled。"""
        return await self._lifecycle.settle_cancelled(
            run_id,
            worker_id,
            execution_epoch,
            settled_at,
            llm_request_count=llm_request_count,
        )

    async def cancel(
        self, session_id: str, owner_id: str, run_id: str, now: datetime
    ) -> Run | None:
        """请求取消或直接终结尚未执行的 Run。"""
        return await self._lifecycle.cancel(session_id, owner_id, run_id, now)

    async def retry(
        self,
        session_id: str,
        owner_id: str,
        source_run_id: str,
        request_id: str,
        fingerprint: str,
        new_run_id: str,
        prompt_template_version: str,
        now: datetime,
        confirm_unknown: bool = False,
        auth_session_id: str | None = None,
    ) -> Run | None:
        """按原始 trigger/context 建立失败 Run 的新尝试。"""
        return await self._lifecycle.retry(
            session_id,
            owner_id,
            source_run_id,
            request_id,
            fingerprint,
            new_run_id,
            prompt_template_version,
            now,
            confirm_unknown=confirm_unknown,
            auth_session_id=auth_session_id,
        )

    async def get(self, session_id: str, owner_id: str, run_id: str) -> Run | None:
        """读取 owner 可见的 Run。"""
        return await self._lifecycle.get(session_id, owner_id, run_id)

    async def get_details(self, session_id: str, owner_id: str, run_id: str) -> RunDetails | None:
        """单会话聚合读取 owner 可见 Run 及其待回答输入、待审批与子任务投影。"""
        return await self._lifecycle.get_details(session_id, owner_id, run_id)

    async def get_lease_state(
        self, session_id: str, owner_id: str, run_id: str
    ) -> RunLeaseState | None:
        """仅查询 status、worker_id 与 execution_epoch 三个标量列并校验活跃 Session 归属。"""
        return await self._lifecycle.get_lease_state(session_id, owner_id, run_id)

    async def list_runs(
        self,
        session_id: str,
        owner_id: str,
        *,
        before_created_at: datetime | None,
        before_id: str | None,
        limit: int,
    ) -> tuple[Run, ...] | None:
        """读取 owner 可见 Session 中稳定倒序排列的一页 Run。"""
        return await self._lifecycle.list_runs(
            session_id,
            owner_id,
            before_created_at=before_created_at,
            before_id=before_id,
            limit=limit,
        )

    async def list_events(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        *,
        after_sequence: int,
        limit: int,
        through_sequence: int | None = None,
    ) -> tuple[RunEvent, ...]:
        """按 Run 内序号读取状态事件，可限制在固定快照游标内。"""
        return await self._context_events.list_events(
            session_id,
            owner_id,
            run_id,
            after_sequence=after_sequence,
            limit=limit,
            through_sequence=through_sequence,
        )

    async def list_visible_events(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        *,
        after_sequence: int,
        limit: int,
        through_sequence: int | None = None,
    ) -> tuple[RunEvent, ...] | None:
        """单会话内校验 Run 可见性并分页读取状态事件，不可见时返回 None。"""
        return await self._context_events.list_visible_events(
            session_id,
            owner_id,
            run_id,
            after_sequence=after_sequence,
            limit=limit,
            through_sequence=through_sequence,
        )

    async def get_pending_input(
        self, session_id: str, owner_id: str, run_id: str
    ) -> RunInputRequest | None:
        """读取 owner 可见 Run 当前待回答的输入请求。"""
        return await self._lifecycle.get_pending_input(session_id, owner_id, run_id)

    async def get_input_call(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        input_request_id: str,
    ) -> RunInputCall | None:
        """读取该输入请求唯一关联的 request_user_input 调用，包括已回答请求。"""
        return await self._lifecycle.get_input_call(session_id, owner_id, run_id, input_request_id)

    async def list_context_messages(
        self, session_id: str, owner_id: str, run_id: str
    ) -> tuple[ConversationMessage | ExecutionMessage, ...] | None:
        """按已固定 cutoff 读取 Run 的消息上下文，None 表示不可见。"""
        return await self._context_events.list_context_messages(session_id, owner_id, run_id)

    async def list_successfully_loaded_skill_ids(self, run_id: str) -> tuple[str, ...]:
        """从本 Run 已成功完成的 skill_load 调用恢复活跃加载证据。"""
        return await self._context_events.list_successfully_loaded_skill_ids(run_id)

    async def list_context_summaries(
        self, session_id: str, owner_id: str, run_id: str
    ) -> tuple[ContextSummary, ...] | None:
        """读取当前 Run 边界内可用的 Session 摘要版本。"""
        return await self._context_events.list_context_summaries(session_id, owner_id, run_id)

    async def save_context_summary(
        self, command: SaveContextSummaryCommand
    ) -> tuple[ContextSummary, RunEvent] | None:
        """在有效租约下原子提交摘要版本和完成事件。"""
        return await self._context_events.save_context_summary(command)

    async def append_compression_event(
        self, run_id: str, worker_id: str, execution_epoch: int, payload: RunEventPayload
    ) -> RunEvent | None:
        """在有效执行权下持久化压缩状态事件。"""
        return await self._context_events.append_compression_event(
            run_id, worker_id, execution_epoch, payload
        )

    async def get_stream_snapshot(
        self, session_id: str, owner_id: str, run_id: str
    ) -> RunStreamSnapshot | None:
        """在同一数据库快照中读取 Run、正式消息与状态事件游标。"""
        return await self._context_events.get_stream_snapshot(session_id, owner_id, run_id)

    async def is_live_execution(self, run_id: str, execution_epoch: int) -> bool:
        """按数据库时钟校验本代执行仍处于 running 且租约有效。"""
        return await self._lifecycle.is_live_execution(run_id, execution_epoch)

    async def begin_subagent_batch(
        self, command: BeginSubagentBatchCommand
    ) -> BeginSubagentBatchResult:
        """原子创建子 Run 批次并将主 Run 置为 waiting_for_subagents 且释放租约。"""
        return await self._subagents.begin_subagent_batch(command)

    async def count_subagent_dispatches(self, root_run_id: str) -> int:
        """统计同一主 Run 累计已派发的子 Run 数量。"""
        return await self._subagents.count_subagent_dispatches(root_run_id)

    async def list_subagent_tasks(
        self, session_id: str, owner_id: str, parent_run_id: str
    ) -> tuple[SubagentTaskSummary, ...]:
        """读取主 Run 下属全部子任务的聚合状态投影。"""
        return await self._context_events.list_subagent_tasks(session_id, owner_id, parent_run_id)

    async def list_pending_subagent_inputs(
        self, session_id: str, owner_id: str, parent_run_id: str
    ) -> tuple[PendingSubagentInput, ...]:
        """读取主 Run 下属子 Run 当前待回答的澄清请求。"""
        return await self._context_events.list_pending_subagent_inputs(
            session_id, owner_id, parent_run_id
        )

    async def list_pending_subagent_approvals(
        self, session_id: str, owner_id: str, parent_run_id: str
    ) -> tuple[PendingSubagentApproval, ...]:
        """读取主 Run 下属子 Run 当前待人工审批的工具调用。"""
        return await self._context_events.list_pending_subagent_approvals(
            session_id, owner_id, parent_run_id
        )


__all__ = ["SqlAlchemyRunRepository"]
