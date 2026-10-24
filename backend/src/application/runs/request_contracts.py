"""运行请求中跨用例共享的具体数据类型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RootAttachment(BaseModel):
    """当前请求中已验证的附件元数据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    filename: str
    mime_type: str
    size_bytes: int = Field(ge=0)
    status: Literal["ready"]


class RootKnowledgeBase(BaseModel):
    """Root 可选择的知识库描述和可用文档数量。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    name: str
    description: str = ""
    document_count: int = Field(ge=0)
    ready_document_count: int = Field(ge=0)


class RootRequestHistoryMessage(BaseModel):
    """checkpoint 中一条历史对话消息。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    role: Literal["user", "assistant"]
    content: str
    message_id: str | None = None
    timestamp: str | None = None


class RootRequestState(BaseModel):
    """Root 请求在 checkpoint 中保存的 JSON 内容。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    session_id: str = ""
    run_id: str = ""
    message_id: str = ""
    user_message: str = ""
    attachment_ids: list[str] = Field(default_factory=list)
    attachment_refs: list[RootAttachment] = Field(default_factory=list)
    history: list[RootRequestHistoryMessage] = Field(default_factory=list)
    knowledge_bases: list[RootKnowledgeBase] = Field(default_factory=list)

__all__ = [
    "RootAttachment",
    "RootKnowledgeBase",
    "RootRequestHistoryMessage",
    "RootRequestState",
]
