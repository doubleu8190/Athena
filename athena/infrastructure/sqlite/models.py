"""SQLite SQLAlchemy ORM 模型。

所有模型采用软删除策略，包含 deleted_time 字段。
不使用 relationship()，级联操作在 Repository 层手动处理。

名称以 ``_json`` 结尾的字段在数据库中仍保存为文本，这样可以兼容已有的 SQLite 数据库。
Repository 读取后，会把文本转换成 ``athena.models.json_models`` 中对应的业务模型。
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """ORM 模型基类."""

    pass


class AgentRunModel(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (Index("idx_agent_runs_session_status", "session_id", "status"),)
    run_id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="运行唯一标识"
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id"), comment="所属会话标识"
    )
    created_by_command_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="创建该运行的命令标识"
    )
    parent_run_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="父运行标识；Root Run 为空"
    )
    root_run_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="所属 Root Run；Root Run 等于自身标识"
    )
    role: Mapped[str] = mapped_column(String, default="root", comment="运行角色")
    plan_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="所属执行计划"
    )
    task_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="Worker 执行的稳定任务标识"
    )
    attempt: Mapped[int] = mapped_column(
        Integer, default=1, comment="同一任务的执行尝试序号"
    )
    depth: Mapped[int] = mapped_column(
        Integer, default=0, comment="编排深度；Root 为 0，Worker 为 1"
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
        ForeignKey("sessions.id"), comment="所属会话标识"
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
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
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
    __table_args__ = (Index("idx_agent_task_results_plan", "plan_id"),)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.task_id"), primary_key=True
    )
    plan_id: Mapped[str] = mapped_column(String)
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
            sqlite_where=text("stream_id IS NOT NULL AND chunk_id IS NOT NULL"),
        ),
        Index("idx_agent_events_run", "run_id"),
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id"), primary_key=True, comment="所属会话标识"
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


class StreamSnapshotModel(Base):
    __tablename__ = "stream_snapshots"
    stream_id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="流快照唯一标识"
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id"), comment="所属会话标识"
    )
    run_id: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="关联运行标识"
    )
    stream_type: Mapped[str] = mapped_column(
        String, default="answer", comment="流类型，例如 answer 或 thinking"
    )
    version: Mapped[int] = mapped_column(Integer, default=0, comment="快照版本号")
    last_chunk_id: Mapped[int] = mapped_column(
        Integer, default=0, comment="快照覆盖到的最后一个 Chunk 序号"
    )
    content: Mapped[str] = mapped_column(Text, default="", comment="当前完整内容")
    content_length: Mapped[int] = mapped_column(
        Integer, default=0, comment="内容 UTF-8 字节长度"
    )
    status: Mapped[str] = mapped_column(
        String, default="streaming", comment="流快照状态"
    )
    updated_at: Mapped[str] = mapped_column(String, comment="最后更新时间（UTC）")


class ApprovalRecordModel(Base):
    __tablename__ = "approvals"
    __table_args__ = (Index("idx_approvals_session_status", "session_id", "status"),)
    approval_id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="审批记录唯一标识"
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id"), comment="所属会话标识"
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
    type: Mapped[str | None] = mapped_column(String, nullable=True, comment="消息类型")
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
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
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

    __tablename__ = "tool_call"
    __table_args__ = (
        Index("idx_tool_call_session", "session_id"),
        Index("idx_tool_call_step", "step_id"),
        Index("idx_tool_call_status", "status"),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="工具调用记录唯一标识"
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id"), comment="所属会话标识"
    )
    # Nullable keeps databases created before step observability readable;
    # new writes always provide a step_id and the startup migration adds this
    # column to legacy tool_call tables when it is missing.
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


class ApprovalLogModel(Base):
    """审批日志表模型."""

    __tablename__ = "approval_logs"
    __table_args__ = (Index("idx_approval_logs_session", "session_id"),)

    id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="审批日志唯一标识"
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id"), comment="所属会话标识"
    )
    tool_call_id: Mapped[str] = mapped_column(String, comment="工具调用标识")
    tool_name: Mapped[str] = mapped_column(String, comment="工具名称")
    arguments_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="工具调用参数（JSON 格式）"
    )
    risk_level: Mapped[str] = mapped_column(String, comment="工具风险等级")
    decision: Mapped[str] = mapped_column(String, comment="审批决定")
    decision_time_ms: Mapped[float] = mapped_column(
        Float, default=0, comment="审批处理耗时（毫秒）"
    )
    timestamp: Mapped[str] = mapped_column(String, comment="日志时间（UTC）")
    deleted_time: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="软删除时间（UTC）"
    )


class MemoryModel(Base):
    """记忆表模型 — 与 ChromaDB 双写的 SQLite 侧.

    用于 FTS5 全文检索（关键词检索），ChromaDB 负责向量检索。
    content 字段通过 FTS5 虚拟表 memory_fts 建立全文索引。
    """

    __tablename__ = "memories"
    __table_args__ = (Index("idx_memories_session", "session_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, comment="记忆唯一标识")
    session_id: Mapped[str] = mapped_column(String, comment="所属会话标识")
    content: Mapped[str] = mapped_column(Text, comment="记忆内容")
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
    last_accessed: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="最后访问时间（UTC）"
    )
    access_count: Mapped[int] = mapped_column(Integer, default=0, comment="被访问次数")
    # 语义字段（平铺自 metadata_json，支持 SQL 过滤）
    type: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="记忆类型，例如 fact 或 summary"
    )
    category: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="记忆分类"
    )
    confidence: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="记忆置信度"
    )
    source: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="记忆来源，例如 extraction、threshold 或 api",
    )
    status: Mapped[str] = mapped_column(
        String, default="active", comment="生命周期状态"
    )
    superseded_by: Mapped[str | None] = mapped_column(String, nullable=True)
    superseded_at: Mapped[str | None] = mapped_column(String, nullable=True)
    source_turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_observed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    deleted_time: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="软删除时间（UTC）"
    )


class McpServerModel(Base):
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
    description / parameters_json 以注册时的最新值为准（MCP 工具可能随 服务端 升级而变化），
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
    parameters_json: Mapped[str] = mapped_column(
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
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True, comment="软删除时间（UTC）")


class SessionKnowledgeBaseModel(Base):
    """会话与可访问知识库的多对多绑定。"""

    __tablename__ = "session_knowledge_bases"
    __table_args__ = (Index("idx_session_knowledge_bases_kb", "knowledge_base_id"),)

    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), primary_key=True)
    created_at: Mapped[str] = mapped_column(String, comment="绑定时间（UTC）")


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
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id"), nullable=True, comment="所属会话标识"
    )
    knowledge_base_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_bases.id"), nullable=True, comment="所属知识库标识"
    )
    message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id"), nullable=True, comment="关联消息标识"
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
    metadata_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="附件信息（JSON 格式）"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="附件处理错误信息"
    )
    created_at: Mapped[str] = mapped_column(String, comment="创建时间（UTC）")
    updated_at: Mapped[str] = mapped_column(String, comment="最后更新时间（UTC）")
    deleted_time: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="软删除时间（UTC）"
    )


class FileChunkModel(Base):
    """文件内容分块的 ORM 映射。"""

    __tablename__ = "file_chunks"
    __table_args__ = (
        UniqueConstraint("attachment_id", "ordinal", name="uq_file_chunk_ordinal"),
        Index("idx_file_chunks_attachment", "attachment_id"),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="文件分块唯一标识"
    )
    attachment_id: Mapped[str] = mapped_column(
        ForeignKey("attachments.id"), comment="所属附件标识"
    )
    ordinal: Mapped[int] = mapped_column(Integer, comment="分块在附件中的顺序号")
    content: Mapped[str] = mapped_column(Text, comment="分块文本内容")
    token_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="分块估算 Token 数"
    )
    locator_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="分块在原文件中的定位信息 JSON"
    )
    metadata_json: Mapped[str] = mapped_column(
        Text, default="{}", comment="分块信息（JSON 格式）"
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


# FTS5 虚拟表 DDL（SQLAlchemy ORM 不支持 FTS5，需通过原生 SQL 创建）
MEMORY_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content,
    memory_id UNINDEXED,
    tokenize='unicode61'
)
"""

FILE_CHUNK_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS file_chunk_fts USING fts5(
    content,
    chunk_id UNINDEXED,
    attachment_id UNINDEXED,
    tokenize='unicode61'
)
"""
