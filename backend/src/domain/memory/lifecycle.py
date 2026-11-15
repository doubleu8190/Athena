"""记忆生命周期、候选决策、后台任务与显式控制的类型化契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class LoadPolicy(StrEnum):
    """长期记忆是否默认参与每轮上下文装配。"""

    ALWAYS = "always"
    ON_DEMAND = "on_demand"


class MemoryKind(StrEnum):
    """长期记忆的业务内容类型。"""

    CONSTRAINT = "constraint"
    DECISION = "decision"
    PREFERENCE = "preference"
    FACT = "fact"


class ExtractionStatus(StrEnum):
    """Run 的用户证据是否已经完成记忆处理。"""

    PENDING = "pending"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class JobKind(StrEnum):
    """五张记忆表中的单一任务分类。"""

    EXTRACT_PENDING = "extract_pending"
    MAINTAIN_SCOPE = "maintain_scope"
    CLEANUP_SCOPE = "cleanup_scope"
    CONTROL = "control"
    SCAN_RUNS = "scan_runs"


class JobStatus(StrEnum):
    """后台任务与同步回执的统一状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class DecisionAction(StrEnum):
    """批量决策允许的三种结果。"""

    ADD = "ADD"
    UPDATE = "UPDATE"
    IGNORE = "IGNORE"


class MemoryCandidate(BaseModel):
    """首轮模型提出、仍待来源与权限校验的长期记忆候选。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str = Field(min_length=1, max_length=2000)
    kind: MemoryKind = MemoryKind.FACT
    scope: Literal["personal", "project"] = "personal"
    source_message_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    evidence: str = Field(min_length=1, max_length=2000)
    proposed_load_policy: LoadPolicy = LoadPolicy.ON_DEMAND
    classification_reason: str = Field(default="", max_length=500)
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _normalize_sources(self) -> MemoryCandidate:
        """同一候选不得重复引用同一消息，避免重复来源伪造额外证据。"""
        if len(set(self.source_message_ids)) != len(self.source_message_ids):
            raise ValueError("memory_candidate_sources_duplicate")
        return self


class ExtractionModelOutput(BaseModel):
    """首轮联合提取的严格输出。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: tuple[MemoryCandidate, ...] = Field(default=(), max_length=100)


class MemoryDecision(BaseModel):
    """批量模型针对一个服务端候选索引给出的原始决策。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_index: int = Field(ge=0)
    action: DecisionAction
    target_memory_id: str | None = Field(default=None, max_length=64)
    content: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _validate_target(self) -> MemoryDecision:
        """只有 UPDATE 可以携带目标，IGNORE 不得携带待写正文。"""
        if self.action is DecisionAction.UPDATE:
            if self.target_memory_id is None:
                raise ValueError("memory_decision_target_required")
        elif self.target_memory_id is not None:
            raise ValueError("memory_decision_target_forbidden")
        if self.action is DecisionAction.IGNORE and self.content is not None:
            raise ValueError("memory_decision_ignore_content_forbidden")
        return self


class MemoryDecisionOutput(BaseModel):
    """一次批量决策调用必须覆盖的服务端候选索引集合。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decisions: tuple[MemoryDecision, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def _validate_indexes(self) -> MemoryDecisionOutput:
        """输出层先拒绝重复索引，完整覆盖在服务端结合候选数复核。"""
        indexes = [item.candidate_index for item in self.decisions]
        if len(set(indexes)) != len(indexes):
            raise ValueError("memory_decision_index_duplicate")
        return self


@dataclass(frozen=True, slots=True)
class MemoryContextItem:
    """进入当前模型请求的完整记忆条目引用。"""

    memory_id: str
    version: int
    scope_key: str
    content: str
    kind: MemoryKind
    load_policy: LoadPolicy
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class MemoryContextSnapshot:
    """一次请求前的读取开关和按 ID 复核结果。"""

    read_enabled: bool
    items: dict[str, MemoryContextItem]


type MemoryReferenceKey = tuple[str, int, str]


class RunMemoryContextReference(BaseModel):
    """Run 首次装配后固定复用的记忆引用，不持久化正文。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    scope_key: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^(?:personal|project:[^\s]+)$",
    )


class RunMemoryContextState(BaseModel):
    """区分未初始化与已初始化为空的 Run 固定记忆快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    initialized: bool = False
    references: tuple[RunMemoryContextReference, ...] = ()

    @model_validator(mode="after")
    def _validate_references(self) -> RunMemoryContextState:
        """未初始化时不得伪造引用，同一记忆 ID 只能固定一个版本和 scope。"""
        if not self.initialized and self.references:
            raise ValueError("run_memory_context_state_invalid")
        memory_ids = [item.memory_id for item in self.references]
        if len(set(memory_ids)) != len(memory_ids):
            raise ValueError("run_memory_context_reference_duplicate")
        return self


@dataclass(frozen=True, slots=True)
class MemoryReferenceSnapshot:
    """历史工具结果按精确 ID/版本/scope 复核后的可注入正文。"""

    read_enabled: bool
    items: dict[MemoryReferenceKey, MemoryContextItem]


class _JobPayload(BaseModel):
    """所有持久化任务参数的公共严格配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ExtractPendingPayload(_JobPayload):
    """用户级提取任务只保存用户身份，批次内容由 Worker 在内存中构造。"""

    kind: Literal[JobKind.EXTRACT_PENDING] = JobKind.EXTRACT_PENDING
    owner_id: str = Field(min_length=1, max_length=128)


class MaintainScopePayload(_JobPayload):
    """单个 scope 的每日归档任务。"""

    kind: Literal[JobKind.MAINTAIN_SCOPE] = JobKind.MAINTAIN_SCOPE
    namespace_id: str = Field(min_length=1, max_length=128)
    scope_key: str = Field(min_length=1, max_length=128)
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class CleanupScopePayload(_JobPayload):
    """项目删除后清理候选正文的幂等任务。"""

    kind: Literal[JobKind.CLEANUP_SCOPE] = JobKind.CLEANUP_SCOPE
    namespace_id: str = Field(min_length=1, max_length=128)
    scope_key: str = Field(min_length=1, max_length=128)


class ScanRunsPayload(_JobPayload):
    """固定扫描记录保存的有界游标和轮次。"""

    kind: Literal[JobKind.SCAN_RUNS] = JobKind.SCAN_RUNS
    cursor_finished_at: datetime | None = None
    cursor_run_id: str | None = Field(default=None, max_length=128)
    scan_round: int = Field(default=0, ge=0)
    enabled_boundary_at: datetime


class ControlPayload(_JobPayload):
    """同步控制回执只保存规范参数摘要与目标元数据。"""

    kind: Literal[JobKind.CONTROL] = JobKind.CONTROL
    owner_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    tool_call_id: str = Field(min_length=1, max_length=256)
    operation: Literal["remember", "correct", "forget", "clear_scope"]
    arguments_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    auth_session_id: str = Field(min_length=1, max_length=128)
    namespace_id: str = Field(min_length=1, max_length=128)
    scope_key: str = Field(min_length=1, max_length=128)


type MemoryJobPayload = Annotated[
    ExtractPendingPayload
    | MaintainScopePayload
    | CleanupScopePayload
    | ScanRunsPayload
    | ControlPayload,
    Field(discriminator="kind"),
]


class ControlReceiptResult(BaseModel):
    """响应丢失后可重放的无正文控制结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["remember", "correct", "forget", "clear_scope"]
    memory_id: str | None = None
    superseded_memory_id: str | None = None
    generation: int = Field(ge=1)


class ExtractBatchResult(BaseModel):
    """批次完成后清理正文前仍保留的无正文审计摘要。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str = Field(min_length=1, max_length=128)
    run_ids: tuple[str, ...] = ()
    created: int = Field(default=0, ge=0)
    updated: int = Field(default=0, ge=0)
    ignored: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    status: Literal["applied", "discarded"] = "applied"
    discard_reason: str | None = Field(default=None, max_length=128)


class MaintainResult(BaseModel):
    """每日维护的无正文统计。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expired: int = Field(default=0, ge=0)
    age_archived: int = Field(default=0, ge=0)


class CleanupResult(BaseModel):
    """scope 清理的无正文统计。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cleared_jobs: int = Field(default=0, ge=0)
    cleared_memories: int = Field(default=0, ge=0)


class ScanResult(BaseModel):
    """扫描页的无正文统计。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scanned: int = Field(default=0, ge=0)
    awakened: int = Field(default=0, ge=0)
    has_more: bool = False
    next_cursor_finished_at: datetime | None = None
    next_cursor_run_id: str | None = Field(default=None, max_length=128)
    round_complete: bool = False


@dataclass(frozen=True, slots=True)
class MemoryJob:
    """Worker 领取到的类型化记忆任务。"""

    id: str
    dedupe_key: str
    kind: JobKind
    status: JobStatus
    payload: MemoryJobPayload
    result: dict[str, Any] | None
    namespace_id: str
    scope_key: str | None
    attempt_count: int
    schema_retry_count: int
    available_at: datetime
    created_at: datetime
    updated_at: datetime


class ExtractBatchMember(_JobPayload):
    """固定批次中的一个 Run 及其全部输入消息，不持久化用户正文。"""

    run_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=64)
    project_id: str | None = Field(default=None, max_length=64)
    source_message_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    source_cutoff_at: datetime

    @model_validator(mode="after")
    def _validate_sources(self) -> ExtractBatchMember:
        """Run 来源必须去重，避免同一证据重复计入。"""
        if len(set(self.source_message_ids)) != len(self.source_message_ids):
            raise ValueError("memory_batch_member_sources_duplicate")
        return self


class ExtractionDecision(_JobPayload):
    """服务端补齐 scope、版本和来源后的批量决策。"""

    candidate_index: int = Field(ge=0)
    candidate: MemoryCandidate
    action: DecisionAction
    content: str = Field(min_length=1, max_length=2000)
    namespace_id: str = Field(min_length=1, max_length=128)
    scope_key: str = Field(min_length=1, max_length=128)
    source_message_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    target_memory_id: str | None = Field(default=None, max_length=64)
    target_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_decision(self) -> ExtractionDecision:
        """动作、目标和来源必须保持一致，避免结果被扩成宽权限计划。"""
        if len(set(self.source_message_ids)) != len(self.source_message_ids):
            raise ValueError("memory_extraction_decision_sources_duplicate")
        if self.action is DecisionAction.ADD:
            if self.target_memory_id is not None or self.target_version is not None:
                raise ValueError("memory_extraction_decision_target_invalid")
        elif self.action is DecisionAction.UPDATE and (
            self.target_memory_id is None or self.target_version is None
        ):
            raise ValueError("memory_extraction_decision_target_invalid")
        elif self.action is DecisionAction.IGNORE and (
            self.target_memory_id is not None or self.target_version is not None
        ):
            raise ValueError("memory_extraction_decision_target_invalid")
        return self


class ExtractBatchPayload(_JobPayload):
    """调用模型前固定在内存中的联合提取批次。"""

    batch_id: str = Field(min_length=1, max_length=128)
    owner_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=1)
    members: tuple[ExtractBatchMember, ...] = Field(min_length=1, max_length=20)
    model_profile_version_id: str = Field(min_length=1, max_length=64)
    prompt_version: Literal[2] = 2
    scope_keys: tuple[str, ...] = Field(min_length=1, max_length=20)
    created_at: datetime
    candidates: tuple[MemoryCandidate, ...] = Field(default=(), max_length=100)
    extraction_complete: bool = False
    schema_retried: bool = False
    decisions: tuple[ExtractionDecision, ...] = Field(default=(), max_length=100)
    decisions_complete: bool = False
    skipped: int = Field(default=0, ge=0, le=100)
    # 仅在内存中传递候选检索阶段已生成的向量，避免 prepare_changes 重复向量化；不参与序列化。
    candidate_embeddings: tuple[tuple[float, ...], ...] = Field(default=(), exclude=True)

    @model_validator(mode="after")
    def _validate_batch(self) -> ExtractBatchPayload:
        """批次成员、scope 与来源均须稳定唯一。"""
        member_ids = [item.run_id for item in self.members]
        if len(set(member_ids)) != len(member_ids):
            raise ValueError("memory_batch_member_duplicate")
        if len(set(self.scope_keys)) != len(self.scope_keys):
            raise ValueError("memory_batch_scope_duplicate")
        member_scopes = {
            "personal" if member.project_id is None else f"project:{member.project_id}"
            for member in self.members
        }
        # 项目组同时固定项目 scope 和可选的 personal 写回范围；成员来源只落在
        # 项目 scope，因此成员 scope 必须被批次 scope 覆盖，不能反向要求相等。
        if not member_scopes.issubset(set(self.scope_keys)):
            raise ValueError("memory_batch_member_scope_mismatch")
        source_ids = {
            source_id
            for member in self.members
            for source_id in member.source_message_ids
        }
        if any(
            not set(candidate.source_message_ids).issubset(source_ids)
            for candidate in self.candidates
        ):
            raise ValueError("memory_batch_candidate_source_invalid")
        if any(
            not set(decision.source_message_ids).issubset(source_ids)
            for decision in self.decisions
        ):
            raise ValueError("memory_batch_decision_source_invalid")
        if any(
            decision.namespace_id != self.owner_id
            or decision.scope_key not in self.scope_keys
            for decision in self.decisions
        ):
            raise ValueError("memory_batch_decision_scope_invalid")
        if self.decisions and not self.extraction_complete:
            raise ValueError("memory_batch_decisions_before_extraction")
        return self


@dataclass(frozen=True, slots=True)
class ControlCommand:
    """同步控制事务所需的已校验参数和预生成向量。"""

    receipt: ControlPayload
    decision_generation: int
    access_scope_key: str
    operation: Literal["remember", "correct", "forget", "clear_scope"]
    content: str | None
    kind: MemoryKind | None
    load_policy: LoadPolicy | None
    evidence_sources: tuple[MemorySourceMessage, ...]
    evidenced_at: datetime | None
    expires_at: datetime | None
    target_memory_id: str | None
    expected_version: int | None
    clear_scope: bool
    create_memory_id: str | None
    embedding: tuple[float, ...] | None
    embedding_profile_id: str | None


@dataclass(frozen=True, slots=True)
class ApplyBatchCommand:
    """记忆变更、Run 完成和任务完成单事务提交命令。"""

    job_id: str
    batch: ExtractBatchPayload
    changes: tuple[PreparedBatchChange, ...]
    summarized_skips: int = 0
    discard_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CompleteJobCommand:
    """完成、重排或失败一个非提取后台任务。"""

    job_id: str
    result: MemoryJobResult | None
    safe_error_code: str | None = None
    retryable: bool = False
    schema_invalid: bool = False
    failed_run_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ControlSourceSnapshot:
    """Run 当前授权和用户直接证据的可信快照。"""

    owner_id: str
    auth_session_id: str
    session_id: str
    project_id: str | None
    run_id: str
    messages: tuple[MemorySourceMessage, ...]


class MemoryRunWakeupPort(Protocol):
    """Run 终态提交后用于尽力唤醒用户级提取任务。"""

    async def wake_user(self, owner_id: str) -> None:
        """创建或刷新固定用户任务；失败时由周期扫描补偿。"""
        ...


@dataclass(frozen=True, slots=True)
class MemorySourceMessage:
    """从可信 ConversationMessage 读取的原始用户证据。"""

    message_id: str
    text: str
    source_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryPendingRun:
    """待联合提取的 Run、读取时用户代次及其固定来源范围。"""

    run_id: str
    owner_id: str
    session_id: str
    project_id: str | None
    finished_at: datetime
    generation: int
    messages: tuple[MemorySourceMessage, ...]
    context_state: RunMemoryContextState


@dataclass(frozen=True, slots=True)
class PreparedMemoryCreate:
    """事务外完成向量、正文和来源的待创建记忆。"""

    memory_id: str
    namespace_id: str
    request_id: str
    scope_key: str
    content: str
    content_hash: str
    embedding: tuple[float, ...]
    embedding_profile_id: str
    kind: MemoryKind
    load_policy: LoadPolicy
    explicit_long_term: bool
    last_evidenced_at: datetime
    expires_at: datetime | None
    source_evidence: tuple[MemorySourceMessage, ...]
    origin: Literal["explicit", "extracted"] = "explicit"


@dataclass(frozen=True, slots=True)
class PreparedBatchChange:
    """批次最终变更计划；UPDATE 保留旧来源和 superseded 链。"""

    action: DecisionAction
    namespace_id: str
    scope_key: str
    candidate_index: int
    candidate: MemoryCandidate
    content: str
    target_memory_id: str | None
    target_version: int | None
    source_evidence: tuple[MemorySourceMessage, ...]
    create: PreparedMemoryCreate | None = None


class ControlJobResult(BaseModel):
    """control 回执结果，禁止夹带其他任务字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[JobKind.CONTROL] = JobKind.CONTROL
    result: ControlReceiptResult


class ExtractJobResult(BaseModel):
    """提取任务完成摘要，正文在提交后已经清理。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[JobKind.EXTRACT_PENDING] = JobKind.EXTRACT_PENDING
    result: ExtractBatchResult


class MaintainJobResult(BaseModel):
    """维护任务的无正文统计。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[JobKind.MAINTAIN_SCOPE] = JobKind.MAINTAIN_SCOPE
    result: MaintainResult


class CleanupJobResult(BaseModel):
    """清理任务的无正文统计。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[JobKind.CLEANUP_SCOPE] = JobKind.CLEANUP_SCOPE
    result: CleanupResult


class ScanJobResult(BaseModel):
    """扫描任务当前页的无正文统计。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[JobKind.SCAN_RUNS] = JobKind.SCAN_RUNS
    result: ScanResult


type MemoryJobResult = Annotated[
    ControlJobResult | ExtractJobResult | MaintainJobResult | CleanupJobResult | ScanJobResult,
    Field(discriminator="kind"),
]


@dataclass(frozen=True, slots=True)
class MemoryScopeState:
    """单个授权 scope 是否启用的当前快照。"""

    namespace_id: str
    scope_key: str
    enabled: bool


__all__ = [
    "ApplyBatchCommand",
    "CleanupResult",
    "CleanupJobResult",
    "CleanupScopePayload",
    "ControlPayload",
    "ControlCommand",
    "ControlReceiptResult",
    "ControlSourceSnapshot",
    "CompleteJobCommand",
    "DecisionAction",
    "ExtractBatchResult",
    "ExtractBatchMember",
    "ExtractBatchPayload",
    "ExtractionDecision",
    "ExtractJobResult",
    "ExtractPendingPayload",
    "ExtractionModelOutput",
    "ExtractionStatus",
    "JobKind",
    "JobStatus",
    "LoadPolicy",
    "MaintainResult",
    "MaintainScopePayload",
    "MemoryCandidate",
    "MemoryContextItem",
    "MemoryContextSnapshot",
    "MemoryDecision",
    "MemoryDecisionOutput",
    "MemoryJob",
    "MemoryJobPayload",
    "MemoryJobResult",
    "MemoryKind",
    "MemoryPendingRun",
    "MemoryReferenceKey",
    "MemoryReferenceSnapshot",
    "MemoryRunWakeupPort",
    "MemoryScopeState",
    "MemorySourceMessage",
    "PreparedBatchChange",
    "PreparedMemoryCreate",
    "RunMemoryContextReference",
    "RunMemoryContextState",
    "ScanRunsPayload",
    "ScanResult",
    "ScanJobResult",
    "ControlJobResult",
    "MaintainJobResult",
]
