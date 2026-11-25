"""Run 原子命令与 Worker 状态持久化端口。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

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
    RunStreamEvent,
    RunStreamSnapshot,
    SaveContextSummaryCommand,
    SubagentTaskSummary,
    SubmitRunCommand,
    SubmitRunResult,
)
from domain.tool import (
    AdmitToolCallCommand,
    BeginToolCallBatchCommand,
    BeginToolCallBatchResult,
    ConvergeToolBatchCommand,
    DecideToolApprovalCommand,
    FinishToolCallCommand,
    InitializeRunToolsCommand,
    PendingToolApproval,
    ReserveLlmRequestCommand,
    RuntimeToolSnapshot,
    RunToolBatch,
    RunToolCallDetails,
    RunToolCallPage,
    RunToolCallRecord,
    ToolRetryContext,
)


class RunEventTransportError(RuntimeError):
    """实时事件传输暂时不可用；调用方应重连或降级。"""


class RunRepository(Protocol):
    """通过原子命令管理 Run，不向应用层暴露 AsyncSession。"""

    async def submit(
        self, session_id: str, owner_id: str, command: SubmitRunCommand
    ) -> SubmitRunResult:
        """写入触发消息、Run、queued 事件与幂等结果。"""
        ...

    async def answer(self, session_id: str, owner_id: str, command: AnswerRunCommand) -> Run:
        """保存回答及协议结果并将等待 Run 重新排队。"""
        ...

    async def claim(
        self, worker_id: str, *, lease_seconds: int, execution_seconds: int, limit: int
    ) -> tuple[RunLease, ...]:
        """按 Session → Run 锁顺序领取有限数量的 queued Run。"""
        ...

    async def reclaim_expired(self, *, limit: int) -> tuple[Run, ...]:
        """把失租 running/cancelling Run 分别收敛为失败或已取消。"""
        ...

    async def heartbeat(
        self, run_id: str, worker_id: str, execution_epoch: int, *, lease_seconds: int
    ) -> Run | None:
        """仅在数据库时钟下租约仍有效时续约。"""

    async def record_usage(self, command: RecordRunUsageCommand) -> Run | None:
        """按执行代次和模型调用序号幂等记录已知 usage。"""

    async def initialize_tools(
        self, command: InitializeRunToolsCommand
    ) -> RuntimeToolSnapshot | None:
        """有效租约下固定快照；已固定时只返回原快照。"""

    async def reserve_llm_request(self, command: ReserveLlmRequestCommand) -> int | None:
        """原子预留一次模型请求并返回 1–maximum_count 的序号。"""

    async def begin_tool_call_batch(
        self, command: BeginToolCallBatchCommand
    ) -> BeginToolCallBatchResult:
        """原子保存助手调用消息、全部调用记录和审批等待点。"""
        ...

    async def get_resumable_tool_batch(self, run_id: str) -> RunToolBatch | None:
        """在任何新模型请求前读取已批准但尚未执行的批次。"""

    async def admit_tool_call(self, command: AdmitToolCallCommand) -> RunToolCallRecord | None:
        """以当前 Worker 和代次原子取得单调用外部执行准入。"""

    async def converge_tool_batch(self, command: ConvergeToolBatchCommand) -> Run | None:
        """停止派发、取消未执行调用并原子失败整个 Run。"""

    async def decide_tool_approval(self, command: DecideToolApprovalCommand) -> Run | None:
        """逐调用批准/拒绝，最后一个决定原子重新排队 Run。"""

    async def list_pending_approvals(
        self, session_id: str, owner_id: str, run_id: str
    ) -> tuple[PendingToolApproval, ...] | None:
        """读取 owner 可见 Run 的安全待审批调用投影。"""

    async def finish_tool_call(self, command: FinishToolCallCommand) -> Run | None:
        """有效执行权内幂等保存唯一结果和结束事件。"""

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

    async def get_tool_call(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        call_id: str,
    ) -> RunToolCallDetails | None:
        """读取授权调用详情，不暴露内部 Server 或异常。"""

    async def list_retry_context(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
    ) -> tuple[ToolRetryContext, ...] | None:
        """读取当前 Run 来源 unknown 调用的安全摘要。"""

    async def request_input(self, command: RequestRunInputCommand) -> RunInputRequest:
        """原子保存澄清消息、调用轨迹、输入请求与 waiting 状态事件。"""
        ...

    async def finish(self, command: FinishRunCommand) -> Run | None:
        """校验执行租约并原子保存最终回复、用量和终态事件。"""

    async def fail(self, command: FailRunCommand) -> Run | None:
        """校验执行租约并原子保存安全失败状态和事件。"""

    async def settle_cancelled(
        self, run_id: str, worker_id: str, execution_epoch: int, settled_at: datetime
    ) -> Run | None:
        """在取消清理完成后把当前 cancelling Run 原子收敛为 cancelled。"""

    async def cancel(
        self, session_id: str, owner_id: str, run_id: str, now: datetime
    ) -> Run | None:
        """请求取消或直接终结尚未执行的 Run。"""

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

    async def get(self, session_id: str, owner_id: str, run_id: str) -> Run | None:
        """读取 owner 可见的 Run。"""

    async def get_details(self, session_id: str, owner_id: str, run_id: str) -> RunDetails | None:
        """单会话聚合读取 owner 可见 Run 及其待回答输入、待审批与子任务投影。"""
        ...

    async def get_lease_state(
        self, session_id: str, owner_id: str, run_id: str
    ) -> RunLeaseState | None:
        """仅查询 status、worker_id 与 execution_epoch 三个标量列并校验活跃 Session 归属。"""
        ...

    async def list_runs(
        self,
        session_id: str,
        owner_id: str,
        *,
        before_created_at: datetime | None,
        before_id: str | None,
        limit: int,
    ) -> tuple[Run, ...] | None:
        """读取可见 Session 中稳定倒序排列的一页 Run。"""

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
        ...

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
        ...

    async def get_pending_input(
        self, session_id: str, owner_id: str, run_id: str
    ) -> RunInputRequest | None:
        """读取 owner 可见 Run 当前待回答的输入请求。"""

    async def get_input_call(
        self,
        session_id: str,
        owner_id: str,
        run_id: str,
        input_request_id: str,
    ) -> RunInputCall | None:
        """读取该输入请求唯一关联的 request_user_input 调用，包括已回答请求。"""

    async def list_context_messages(
        self, session_id: str, owner_id: str, run_id: str
    ) -> tuple[ConversationMessage | ExecutionMessage, ...] | None:
        """按已固定 cutoff 读取 Run 的消息上下文，None 表示不可见。"""

    async def list_successfully_loaded_skill_ids(self, run_id: str) -> tuple[str, ...]:
        """从本 Run 已成功完成的 skill_load 调用恢复活跃加载证据。"""

    async def list_context_summaries(
        self, session_id: str, owner_id: str, run_id: str
    ) -> tuple[ContextSummary, ...] | None:
        """读取当前 Run 边界内可用的 Session 摘要版本。"""

    async def save_context_summary(
        self, command: SaveContextSummaryCommand
    ) -> tuple[ContextSummary, RunEvent] | None:
        """在有效租约下原子提交摘要版本和完成事件。"""

    async def append_compression_event(
        self, run_id: str, worker_id: str, execution_epoch: int, payload: RunEventPayload
    ) -> RunEvent | None:
        """在有效执行权下持久化压缩状态事件。"""

    async def get_stream_snapshot(
        self, session_id: str, owner_id: str, run_id: str
    ) -> RunStreamSnapshot | None:
        """在同一数据库快照中读取 Run、正式消息与状态事件游标。"""

    async def is_live_execution(self, run_id: str, execution_epoch: int) -> bool:
        """按数据库时钟校验本代执行仍处于 running 且租约有效。"""
        ...

    async def begin_subagent_batch(
        self, command: BeginSubagentBatchCommand
    ) -> BeginSubagentBatchResult:
        """原子创建子 Run 批次并将主 Run 置为 waiting_for_subagents 且释放租约。"""
        ...

    async def count_subagent_dispatches(self, root_run_id: str) -> int:
        """统计同一主 Run 累计已派发的子 Run 数量。"""
        ...

    async def list_subagent_tasks(
        self, session_id: str, owner_id: str, parent_run_id: str
    ) -> tuple[SubagentTaskSummary, ...]:
        """读取主 Run 下属全部子任务的聚合状态投影。"""
        ...

    async def list_pending_subagent_inputs(
        self, session_id: str, owner_id: str, parent_run_id: str
    ) -> tuple[PendingSubagentInput, ...]:
        """读取主 Run 下属子 Run 当前待回答的澄清请求。"""
        ...

    async def list_pending_subagent_approvals(
        self, session_id: str, owner_id: str, parent_run_id: str
    ) -> tuple[PendingSubagentApproval, ...]:
        """读取主 Run 下属子 Run 当前待人工审批的工具调用。"""
        ...


class RunEventSubscription(Protocol):
    """一个 Run 的实时事件订阅。"""

    async def get(self, *, timeout: float) -> RunStreamEvent | None:
        """读取下一事件；超时返回 None，传输故障抛出 RunEventTransportError。"""

    async def aclose(self) -> None:
        """释放 Pub/Sub 连接。"""


class RunEventTransport(Protocol):
    """Run 临时事件和已提交状态事件的 Pub/Sub 传输端口。"""

    async def publish_text(
        self,
        run_id: str,
        execution_epoch: int,
        text: str,
        *,
        round: int = 0,
    ) -> None:
        """发布不持久化的文本增量；传输故障抛出 RunEventTransportError。"""

    async def publish_persisted(self, run_id: str, execution_epoch: int, event: RunEvent) -> None:
        """通知数据库已经提交的状态事件；传输故障抛出 RunEventTransportError。"""

    def subscribe(self, run_id: str) -> AbstractAsyncContextManager[RunEventSubscription]:
        """建立一个独立 Pub/Sub 订阅上下文；连接故障抛出 RunEventTransportError。"""
        ...


__all__ = ["RunEventTransport", "RunEventTransportError", "RunRepository"]
