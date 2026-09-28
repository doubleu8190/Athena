"""PostgreSQL SQLAlchemy ORM 模型。

所有模型采用软删除策略，包含 deleted_time 字段。
不使用 relationship()，级联操作在 Repository 层手动处理。

名称以 ``_json`` 结尾的字段在数据库中保存为文本，读取后转换为领域模型。
Repository 读取后，会把文本转换成 ``athena.models.json_models`` 中对应的业务模型。
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    CheckConstraint,
    Computed,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, column_property, mapped_column
from sqlalchemy.dialects.postgresql import TSVECTOR
from pgvector.sqlalchemy import Vector


class Base(DeclarativeBase):
    """ORM 模型基类."""

    pass


class EmbeddingMetadataModel(Base):
    """Records the embedding contract used to populate pgvector columns."""

    __tablename__ = "embedding_metadata"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)


memory_relations_table = Table(
    "memory_relations",
    Base.metadata,
    Column("source_memory_id", String, primary_key=True),
    Column("target_memory_id", String, primary_key=True),
    Column("relation_type", String, primary_key=True),
    Column("created_at", String, nullable=False),
    Column("metadata_json", Text, nullable=False, server_default=text("'{}'")),
)


class AgentRunModel(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (Index("idx_agent_runs_session_status", "session_id", "status"),)
    run_id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="运行唯一标识"
    )
    session_id: Mapped[str] = mapped_column(
        String, comment="所属会话标识；由核心库维护"
    )
    created_by_command_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="创建该运行的命令标识"
    )
    parent_run_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="父运行标识；Root Run 为空"
    )
    attempt: Mapped[int] = mapped_column(
        Integer, default=1, comment="同一任务的执行尝试序号"
    )
    status: Mapped[str] = mapped_column(String, default="queued", comment="运行状态")
    pause_requested: Mapped[int] = mapped_column(
        Integer, default=0, comment="是否请求暂停，0 表示否，1 表示是"
    )
    cancel_requested: Mapped[int] = mapped_column(
        Integer, default=0, comment="是否请求取消，0 表示否，1 表示是"
    )
    error: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="运行失败时的错误信息"
    )
    created_at: Mapped[str] = mapped_column(String, comment="创建时间（UTC）")
    updated_at: Mapped[str] = mapped_column(String, comment="最后更新时间（UTC）")


class AgentCommandModel(Base):
    __tablename__ = "agent_commands"
    __table_args__ = (
        Index("idx_agent_commands_status_available", "status", "available_at"),
    )
    command_id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="命令唯一标识和幂等键"
    )
    session_id: Mapped[str] = mapped_column(
        String, comment="所属会话标识；由核心库维护"
    )
    run_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="关联运行标识"
    )
    command_type: Mapped[str] = mapped_column(String, comment="命令类型")
    schema_version: Mapped[int] = mapped_column(
        Integer, default=1, comment="命令协议版本"
    )
    payload_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="命令内容（JSON 格式）"
    )
    payload_hash: Mapped[str] = mapped_column(
        String, comment="命令内容的哈希值，用于幂等校验"
    )
    status: Mapped[str] = mapped_column(
        String, default="pending", comment="命令处理状态"
    )
    attempt: Mapped[int] = mapped_column(
        Integer, default=0, comment="命令已尝试处理次数"
    )
    claimed_at: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="命令领取时间（用于崩溃恢复）"
    )
    available_at: Mapped[str] = mapped_column(
        String, comment="命令可被消费的时间（UTC）"
    )
    result_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="命令成功返回的结果（JSON 格式）"
    )
    error_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="命令处理失败时的错误信息（JSON 格式）"
    )
    issued_at: Mapped[str] = mapped_column(String, comment="命令发出时间（UTC）")


class AgentPlanModel(Base):
    """中心编排计划的持久化记录。"""

    __tablename__ = "agent_plans"
    __table_args__ = (Index("idx_agent_plans_root_run", "root_run_id"),)
    plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String, comment="所属会话标识；由核心库维护"
    )
    root_run_id: Mapped[str] = mapped_column(String)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="planning")
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    plan_json: Mapped[str] = mapped_column(Text)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class AgentTaskModel(Base):
    """计划内稳定任务及其当前调度状态。"""

    __tablename__ = "agent_tasks"
    __table_args__ = (
        Index("idx_agent_tasks_plan_status", "plan_id", "status"),
        Index("idx_agent_tasks_queue", "status", "available_at"),
    )
    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("agent_plans.plan_id"))
    task_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="queued")
    task_json: Mapped[str] = mapped_column(Text)
    worker_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[str] = mapped_column(String)
    claimed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class AgentTaskResultModel(Base):
    """一次任务最终结果的幂等持久化记录。"""

    __tablename__ = "agent_task_results"
    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.task_id"), primary_key=True
    )
    worker_run_id: Mapped[str] = mapped_column(String, unique=True)
    status: Mapped[str] = mapped_column(String)
    result_json: Mapped[str] = mapped_column(Text)
    output_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    completed_at: Mapped[str] = mapped_column(String)


class AgentEventModel(Base):
    __tablename__ = "agent_events"
    __table_args__ = (
        Index(
            "uq_agent_events_stream_chunk",
            "session_id",
            "stream_id",
            "chunk_id",
            unique=True,
            postgresql_where=text("stream_id IS NOT NULL AND chunk_id IS NOT NULL"),
        ),
        Index("idx_agent_events_run", "run_id"),
    )
    session_id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="所属会话标识；由核心库维护"
    )
    session_seq: Mapped[int] = mapped_column(
        Integer, primary_key=True, comment="会话内递增的事件游标"
    )
    run_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="关联运行标识"
    )
    message_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="关联消息标识"
    )
    attachment_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="关联附件标识"
    )
    event_type: Mapped[str] = mapped_column(String, comment="事件类型")
    durability: Mapped[str] = mapped_column(String, comment="事件持久化级别")
    stream_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="关联流标识"
    )
    stream_type: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="流类型，例如 answer 或 thinking"
    )
    chunk_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="流内从 1 开始的数据块序号"
    )
    is_complete: Mapped[int] = mapped_column(
        Integer, default=0, comment="是否为流完成 Chunk"
    )
    parent_run_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="父运行标识"
    )
    transition_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="业务状态转换幂等键"
    )
    payload_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="事件内容（JSON 格式）"
    )
    occurred_at: Mapped[str] = mapped_column(String, comment="事件发生时间（UTC）")


class ApprovalRecordModel(Base):
    __tablename__ = "approvals"
    __table_args__ = (Index("idx_approvals_session_status", "session_id", "status"),)
    approval_id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="审批记录唯一标识"
    )
    session_id: Mapped[str] = mapped_column(
        String, comment="所属会话标识；由核心库维护"
    )
    run_id: Mapped[str] = mapped_column(String, comment="所属运行标识")
    plan_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="审批所属执行计划"
    )
    task_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="审批所属任务"
    )
    worker_run_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="审批所属 Worker 尝试"
    )
    tool_call_id: Mapped[str] = mapped_column(String, comment="待审批的工具调用标识")
    tool_name: Mapped[str] = mapped_column(String, comment="工具名称")
    arguments_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="工具调用参数（JSON 格式）"
    )
    risk_level: Mapped[str] = mapped_column(
        String, default="low", comment="工具风险等级"
    )
    status: Mapped[str] = mapped_column(String, default="pending", comment="审批状态")
    decision: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="审批决定"
    )
    created_at: Mapped[str] = mapped_column(String, comment="创建时间（UTC）")
    expires_at: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="审批截止时间（UTC）"
    )
    decided_at: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="审批决定时间（UTC）"
    )


class SessionModel(Base):
    """会话表模型."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, comment="会话唯一标识")
    title: Mapped[str] = mapped_column(
        String, default="New Session", comment="会话标题"
    )
    status: Mapped[str] = mapped_column(String, default="idle", comment="会话状态")
    run_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="当前关联运行标识"
    )
    created_at: Mapped[str] = mapped_column(String, comment="创建时间（UTC）")
    updated_at: Mapped[str] = mapped_column(String, comment="最后更新时间（UTC）")
    # 压缩相关字段
    compression_summary: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="对话历史压缩摘要"
    )
    last_compressed_message_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="最后一次压缩处理的消息标识"
    )
    last_summarized_message_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="最后一次摘要处理的消息标识"
    )
    deleted_time: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="软删除时间（UTC）"
    )


class MessageModel(Base):
    """消息表模型."""

    __tablename__ = "messages"
    __table_args__ = (
        Index("idx_messages_session", "session_id"),
        Index("idx_messages_timestamp", "timestamp"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, comment="消息唯一标识")
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id"), comment="所属会话标识"
    )
    role: Mapped[str] = mapped_column(String, comment="消息角色")
    content: Mapped[str] = mapped_column(Text, default="", comment="消息正文")
    tool_calls_json: Mapped[str] = mapped_column(
        Text, default="[]", comment="工具调用列表（JSON 格式）"
    )
    tool_call_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="关联工具调用标识"
    )
    run_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="关联运行标识"
    )
    tool_call_record_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="关联工具调用记录标识"
    )
    tool_name: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="工具名称"
    )
    message_type: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="消息类型"
    )
    timestamp: Mapped[str] = mapped_column(String, comment="消息时间（UTC）")
    deleted_time: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="软删除时间（UTC）"
    )


class StepModel(Base):
    """可观测执行步骤，工具调用通过 step_id 归属到这里。"""

    __tablename__ = "steps"
    __table_args__ = (
        Index("idx_steps_session", "session_id"),
        Index("idx_steps_run", "run_id"),
        Index("idx_steps_number", "step_number"),
        Index("idx_steps_parent", "parent_step_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String, comment="所属会话标识；由核心库维护"
    )
    run_id: Mapped[str] = mapped_column(String)
    step_number: Mapped[int] = mapped_column(Integer)
    step_type: Mapped[str] = mapped_column(String)
    parent_step_id: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[str] = mapped_column(String)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0)
    llm_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)


class ToolCallModel(Base):
    """工具调用记录表模型."""

    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("idx_tool_calls_session", "session_id"),
        Index("idx_tool_calls_step", "step_id"),
        Index("idx_tool_calls_status", "status"),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="工具调用记录唯一标识"
    )
    session_id: Mapped[str] = mapped_column(
        String, comment="所属会话标识；由核心库维护"
    )
    # Nullable keeps imported or manually created records readable;
    # new writes always provide a step_id.
    step_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("steps.id"), nullable=True, comment="所属执行步骤"
    )
    run_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="所属运行标识"
    )
    attempt_number: Mapped[int] = mapped_column(
        Integer, default=1, comment="同一模型工具调用的尝试序号"
    )
    approval_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="本次尝试关联的审批记录"
    )
    tool_name: Mapped[str] = mapped_column(String, comment="工具名称")
    arguments_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="工具调用参数（JSON 格式）"
    )
    raw_output: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="工具原始输出"
    )
    status: Mapped[str] = mapped_column(String, comment="工具调用状态")
    started_at: Mapped[str] = mapped_column(String, comment="开始时间（UTC）")
    completed_at: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="完成时间（UTC）"
    )
    duration_ms: Mapped[float] = mapped_column(
        Float, default=0, comment="执行耗时（毫秒）"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="错误消息"
    )
    error_stack: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="错误堆栈"
    )
    deleted_time: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="软删除时间（UTC）"
    )


class MemoryModel(Base):
    """记忆表模型，包含 PostgreSQL 原生 FTS 和 pgvector 索引字段。"""

    __tablename__ = "memories"
    __table_args__ = (
        Index("idx_memories_session", "session_id"),
        Index("idx_memories_content_fts", "content_fts", postgresql_using="gin"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, comment="记忆唯一标识")
    logical_memory_id: Mapped[str] = mapped_column(
        String, index=True, comment="跨 revision 保持不变的逻辑记忆标识"
    )
    session_id: Mapped[str] = mapped_column(String, comment="所属会话标识")
    content: Mapped[str] = mapped_column(Text, comment="记忆内容")
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(1024), nullable=True, comment="记忆内容的 cosine embedding"
    )
    content_fts: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('chinese'::regconfig, content)", persisted=True),
    )
    # 仅存任意用户扩展字段（系统/语义字段一律拆为独立列，避免双源真相）
    metadata_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="用户自定义的额外信息（JSON 格式）"
    )
    pinned: Mapped[int] = mapped_column(
        Integer, default=0, comment="是否固定，0 表示否，1 表示是"
    )
    expires_at: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="过期时间（UTC）"
    )
    created_at: Mapped[str] = mapped_column(String, comment="创建时间（UTC）")
    last_accessed_at: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="最后访问时间（UTC）"
    )
    access_count: Mapped[int] = mapped_column(Integer, default=0, comment="被访问次数")
    # 语义字段（平铺自 metadata_json，支持 SQL 过滤）
    memory_type: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="记忆类型，例如 fact 或 summary"
    )
    category: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="记忆分类"
    )
    confidence: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="记忆置信度"
    )
    source_kind: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="记忆来源，例如 extraction、threshold 或 api",
    )
    status: Mapped[str] = mapped_column(
        String, default="active", comment="生命周期状态"
    )
    superseded_at: Mapped[str | None] = mapped_column(String, nullable=True)
    # 兼容旧 ORM 调用方；替代关系实际唯一存储在 memory_relations。
    superseded_by: Mapped[str | None] = column_property(
        select(memory_relations_table.c.source_memory_id)
        .where(
            memory_relations_table.c.target_memory_id == id,
            memory_relations_table.c.relation_type == "supersedes",
        )
        .correlate_except(memory_relations_table)
        .scalar_subquery()
    )
    source_turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_observed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    # 事实有效性不等同于保留期限或访问热度。invalid 记录保留用于历史追溯，
    # 但不会进入检索上下文。
    validity_status: Mapped[str] = mapped_column(
        String, default="valid", comment="事实有效性：valid、uncertain 或 invalid"
    )
    valid_until: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="事实确认的有效截止时间（UTC）"
    )
    revision_of: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="被本条修订替代的上一版本记忆 ID"
    )
    revision: Mapped[int] = mapped_column(
        Integer, default=1, comment="同一记忆版本链中的修订序号"
    )
    deleted_time: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="软删除时间（UTC）"
    )


class MCPServerModel(Base):
    """MCP 服务器注册表 — 持久化用户注册的 MCP 服务端 配置.

    config_json 存完整配置 {command, args, env}，env 含密钥（如 SERVER_KEY），
    仅在启动子进程时使用，REST 列表接口只返回掩码。
    """

    __tablename__ = "mcp_servers"

    name: Mapped[str] = mapped_column(
        String, primary_key=True, comment="MCP 服务器名称"
    )
    config_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="MCP 服务器配置（JSON 格式）"
    )
    created_at: Mapped[str] = mapped_column(String, comment="创建时间（UTC）")
    deleted_time: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="软删除时间（UTC）"
    )


class ToolModel(Base):
    """工具治理配置表 — 持久化用户对工具的治理参数调优.

    MCP 工具和 Native 工具统一存储，通过 execution_mode 区分。
    description / parameters_schema_json 以注册时的最新值为准（MCP 工具可能随服务端升级而变化），
    risk_level / require_approval / enabled 以用户修改为准。
    """

    __tablename__ = "tools"

    tool_name: Mapped[str] = mapped_column(
        String, primary_key=True, comment="工具唯一名称"
    )
    execution_mode: Mapped[str] = mapped_column(
        String, comment="工具执行模式，例如 native 或 mcp"
    )
    server_name: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="所属 MCP 服务器名称"
    )
    remote_name: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="MCP 服务器中的远程工具名称"
    )
    description: Mapped[str] = mapped_column(Text, default="", comment="工具描述")
    parameters_schema_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="工具参数 Schema（JSON 格式）"
    )
    risk_level: Mapped[str] = mapped_column(
        String, default="medium", comment="工具风险等级"
    )
    require_approval: Mapped[int] = mapped_column(
        Integer, default=1, comment="是否需要审批，0 表示否，1 表示是"
    )
    enabled: Mapped[int] = mapped_column(
        Integer, default=1, comment="是否启用，0 表示否，1 表示是"
    )
    created_at: Mapped[str] = mapped_column(String, comment="创建时间（UTC）")
    updated_at: Mapped[str] = mapped_column(String, comment="最后更新时间（UTC）")


class KnowledgeBaseModel(Base):
    """独立知识库元数据。"""

    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String, primary_key=True, comment="知识库唯一标识")
    name: Mapped[str] = mapped_column(String, comment="知识库名称")
    description: Mapped[str] = mapped_column(Text, default="", comment="知识库说明")
    created_at: Mapped[str] = mapped_column(String, comment="创建时间（UTC）")
    updated_at: Mapped[str] = mapped_column(String, comment="最后更新时间（UTC）")
    deleted_time: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="软删除时间（UTC）"
    )


class AttachmentModel(Base):
    """会话附件或独立知识库文档的元数据。原始字节存储在存储层。"""

    __tablename__ = "attachments"
    __table_args__ = (
        Index("idx_attachments_session", "session_id"),
        Index("idx_attachments_knowledge_base", "knowledge_base_id"),
        Index("idx_attachments_message", "message_id"),
        Index("idx_attachments_hash", "sha256"),
        Index("idx_attachments_status", "status"),
        CheckConstraint(
            "(session_id IS NOT NULL) != (knowledge_base_id IS NOT NULL)",
            name="ck_attachment_single_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, comment="附件唯一标识")
    logical_document_id: Mapped[str] = mapped_column(
        String, index=True, comment="跨文档版本保持不变的逻辑文档标识"
    )
    document_version: Mapped[int] = mapped_column(
        Integer, default=1, comment="逻辑文档版本序号"
    )
    session_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="所属会话标识；由核心库维护"
    )
    knowledge_base_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_bases.id"), nullable=True, comment="所属知识库标识"
    )
    message_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="关联消息标识；由核心库维护"
    )
    filename: Mapped[str] = mapped_column(String, comment="原始文件名")
    mime_type: Mapped[str] = mapped_column(String, comment="文件 MIME 类型")
    size_bytes: Mapped[int] = mapped_column(Integer, comment="文件大小（字节）")
    sha256: Mapped[str] = mapped_column(String, comment="文件内容 SHA-256 摘要")
    storage_key: Mapped[str] = mapped_column(String, comment="文件存储键")
    adapter_name: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="文件处理适配器名称"
    )
    adapter_version: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="文件处理适配器版本"
    )
    status: Mapped[str] = mapped_column(
        String, default="uploaded", comment="附件处理状态"
    )
    capabilities_json: Mapped[str] = mapped_column(
        Text, default="[]", comment="附件支持的能力列表（JSON 格式）"
    )
    parsed_metadata_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="文件解析元数据（JSON 格式）"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="附件处理错误信息"
    )
    created_at: Mapped[str] = mapped_column(String, comment="创建时间（UTC）")
    updated_at: Mapped[str] = mapped_column(String, comment="最后更新时间（UTC）")
    deleted_time: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="软删除时间（UTC）"
    )


class KnowledgeDocumentJobModel(Base):
    """知识库文档解析和索引任务的持久化状态。"""

    __tablename__ = "knowledge_document_jobs"
    __table_args__ = (
        Index("idx_knowledge_document_jobs_queue", "status", "available_at"),
    )

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    attachment_id: Mapped[str] = mapped_column(
        ForeignKey("attachments.id"), unique=True, comment="待处理知识库文档"
    )
    status: Mapped[str] = mapped_column(String, default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[str] = mapped_column(String)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class FileChunkModel(Base):
    """文件内容分块的 ORM 映射。"""

    __tablename__ = "file_chunks"
    __table_args__ = (
        Index(
            "uq_file_chunk_attachment_ordinal_active",
            "attachment_id",
            "ordinal",
            unique=True,
            postgresql_where=text("deleted_time IS NULL"),
        ),
        Index("idx_file_chunks_attachment", "attachment_id"),
        Index("idx_file_chunks_content_fts", "content_fts", postgresql_using="gin"),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="文件分块唯一标识"
    )
    attachment_id: Mapped[str] = mapped_column(
        ForeignKey("attachments.id"), comment="所属附件标识"
    )
    ordinal: Mapped[int] = mapped_column(Integer, comment="分块在附件中的顺序号")
    content: Mapped[str] = mapped_column(Text, comment="分块文本内容")
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(1024), nullable=True, comment="文件分块的 cosine embedding"
    )
    content_fts: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('chinese'::regconfig, content)", persisted=True),
    )
    token_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="分块估算 Token 数"
    )
    locator_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="分块在原文件中的定位信息 JSON"
    )
    metadata_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="分块信息（JSON 格式）"
    )
    deleted_time: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="软删除时间（UTC）"
    )


class FileArtifactModel(Base):
    """文件解析产物的 ORM 映射。"""

    __tablename__ = "file_artifacts"
    __table_args__ = (
        UniqueConstraint("cache_key", name="uq_file_artifact_cache_key"),
        Index("idx_file_artifacts_attachment", "attachment_id"),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="文件产物唯一标识"
    )
    attachment_id: Mapped[str] = mapped_column(
        ForeignKey("attachments.id"), comment="所属附件标识"
    )
    kind: Mapped[str] = mapped_column(String, comment="产物类型")
    cache_key: Mapped[str] = mapped_column(String, comment="产物缓存键")
    content: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="产物文本内容"
    )
    storage_key: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="产物存储键"
    )
    metadata_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="产物信息（JSON 格式）"
    )
    created_at: Mapped[str] = mapped_column(String, comment="创建时间（UTC）")


class AdapterRegistryModel(Base):
    """文件适配器注册信息的 ORM 映射。"""

    __tablename__ = "adapter_registry"

    name: Mapped[str] = mapped_column(String, primary_key=True, comment="适配器名称")
    version: Mapped[str] = mapped_column(String, comment="适配器版本")
    mime_types_json: Mapped[str] = mapped_column(
        Text, default="[]", comment="支持的 MIME 类型列表（JSON 格式）"
    )
    extensions_json: Mapped[str] = mapped_column(
        Text, default="[]", comment="支持的文件扩展名列表（JSON 格式）"
    )
    capabilities_json: Mapped[str] = mapped_column(
        Text, default="[]", comment="适配器支持的能力列表（JSON 格式）"
    )
    enabled: Mapped[int] = mapped_column(
        Integer, default=1, comment="是否启用，0 表示否，1 表示是"
    )
    updated_at: Mapped[str] = mapped_column(String, comment="最后更新时间（UTC）")


class CodeSymbolModel(Base):
    """代码符号索引的 ORM 映射。"""

    __tablename__ = "code_symbols"
    __table_args__ = (
        Index("idx_code_symbols_attachment", "attachment_id"),
        Index("idx_code_symbols_name", "name"),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="代码符号唯一标识"
    )
    attachment_id: Mapped[str] = mapped_column(
        ForeignKey("attachments.id"), comment="所属附件标识"
    )
    path: Mapped[str] = mapped_column(String, comment="源文件路径")
    language: Mapped[str] = mapped_column(String, comment="编程语言")
    name: Mapped[str] = mapped_column(String, comment="符号名称")
    qualified_name: Mapped[str] = mapped_column(String, comment="符号完整限定名称")
    kind: Mapped[str] = mapped_column(String, comment="符号类型")
    start_line: Mapped[int] = mapped_column(Integer, comment="符号起始行号")
    end_line: Mapped[int] = mapped_column(Integer, comment="符号结束行号")
    signature: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="符号声明签名"
    )


class CodeDependencyModel(Base):
    """代码依赖关系索引的 ORM 映射。"""

    __tablename__ = "code_dependencies"
    __table_args__ = (Index("idx_code_deps_attachment", "attachment_id"),)

    id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="代码依赖关系唯一标识"
    )
    attachment_id: Mapped[str] = mapped_column(
        ForeignKey("attachments.id"), comment="所属附件标识"
    )
    source: Mapped[str] = mapped_column(String, comment="依赖源符号或文件")
    target: Mapped[str] = mapped_column(String, comment="依赖目标符号或文件")
    kind: Mapped[str] = mapped_column(
        String, default="reference", comment="依赖关系类型"
    )
    metadata_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="依赖关系的额外信息（JSON 格式）"
    )


class RetrievalRunModel(Base):
    """一次召回轨迹的可复现元数据。

    ``run_id`` 标识本表中的单次召回轨迹；``agent_run_id`` 标识发起召回的
    上游 Agent 执行。一个 Agent 执行可以并行触发 memory、knowledge 和 file
    等多次召回，因此两者必须保持为不同的标识。
    """

    __tablename__ = "retrieval_runs"
    __table_args__ = (
        Index("idx_retrieval_runs_created_at", "created_at"),
        Index("idx_retrieval_runs_scope", "scope"),
        Index("idx_retrieval_runs_session", "session_id"),
        Index("idx_retrieval_runs_agent_run", "agent_run_id"),
        Index("idx_retrieval_runs_message", "message_id"),
    )

    run_id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="本次召回轨迹的唯一标识"
    )
    query: Mapped[str] = mapped_column(Text, comment="原始检索查询")
    scope: Mapped[str] = mapped_column(
        String, comment="检索范围，例如 memory 或 knowledge"
    )
    status: Mapped[str] = mapped_column(String, default="running", comment="运行状态")
    config_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="检索配置（JSON 格式）"
    )
    index_generation: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="参与检索的索引生成标识"
    )
    session_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="发起检索的会话标识"
    )
    agent_run_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="发起召回的上游 Agent 运行标识；一个 Agent run 可对应多条召回轨迹",
    )
    message_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="触发检索的消息标识"
    )
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    selected_count: Mapped[int] = mapped_column(Integer, default=0)
    injected_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, comment="创建时间（UTC）")
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class RetrievalCandidateModel(Base):
    """检索流水线中单个候选的阶段性轨迹。"""

    __tablename__ = "retrieval_candidates"
    __table_args__ = (
        Index("idx_retrieval_candidates_run_stage", "run_id", "stage"),
        Index("idx_retrieval_candidates_source", "source_type", "source_id"),
    )

    candidate_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("retrieval_runs.run_id"), index=True, comment="所属检索运行"
    )
    provider: Mapped[str] = mapped_column(String, comment="候选来源 provider")
    stage: Mapped[str] = mapped_column(String, comment="候选所在阶段")
    source_type: Mapped[str] = mapped_column(
        String, comment="来源类型，例如 memory 或 chunk"
    )
    source_id: Mapped[str] = mapped_column(String, comment="不可变来源 ID")
    logical_source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    native_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    native_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    fused_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fused_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rerank_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rerank_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    filter_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    selected_for_result: Mapped[int] = mapped_column(Integer, default=0)
    injected_into_context: Mapped[int] = mapped_column(Integer, default=0)
    content_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_title: Mapped[str | None] = mapped_column(String, nullable=True)
    locator_json: Mapped[str] = mapped_column(Text, default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String, comment="记录时间（UTC）")
