"""领域模型 — 供 repository 层与业务层使用的类型化数据结构.

- Message/Session/ToolCallRecord/审批Log: repository 查询结果与 save 输入的类型
- 各 StrEnum: 状态/角色/类型枚举
- ToolSchema/ToolResult/审批Request: 工具与审批流程使用的模型
"""

from athena.models.approval import (
    ApprovalDecision,
    ApprovalLog,
    ApprovalRequest,
)
from athena.models.mcp import MCPServer, MCPServerConfig
from athena.models.json_models import (
    CommandPayload,
    ExtensibleJsonModel,
    FileArtifact,
    FileLocator,
    FileMetadata,
    JsonSchema,
    ToolCall,
)
from athena.models.file import (
    AdapterInfo,
    Attachment,
    AttachmentRef,
    AttachmentStatus,
    FileChunk,
    KnowledgeBase,
)
from athena.models.message import Message, MessageRole
from athena.models.session import Session, SessionStatus
from athena.models.tool import (
    RiskLevel,
    ToolCallRecord,
    ToolCallStatus,
    ToolConfig,
    ToolExecutionMode,
    ToolResult,
    ToolSchema,
)

__all__ = [
    # 消息
    "Message",
    "MessageRole",
    # 会话
    "Session",
    "SessionStatus",
    # 工具
    "RiskLevel",
    "ToolExecutionMode",
    "ToolSchema",
    "ToolConfig",
    "ToolResult",
    "ToolCallRecord",
    "ToolCallStatus",
    "ToolCall",
    "JsonSchema",
    # 审批
    "ApprovalDecision",
    "ApprovalLog",
    "ApprovalRequest",
    # MCP
    "MCPServer",
    "MCPServerConfig",
    "AdapterInfo",
    "Attachment",
    "AttachmentRef",
    "AttachmentStatus",
    "FileChunk",
    "KnowledgeBase",
    "FileArtifact",
    "FileLocator",
    "FileMetadata",
    "CommandPayload",
    "ExtensibleJsonModel",
]
