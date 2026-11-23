"""知识库 HTTP 请求与响应结构。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api.dto.access import VisibilityGrantRequest, VisibilityGrantResponse


class KnowledgeBaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    visibility_scope: Literal["company", "department", "personal"] = "personal"
    owner_department_id: str | None = Field(default=None, min_length=1, max_length=64)
    visibility_grants: list[VisibilityGrantRequest] | None = None


class KnowledgeBasePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    visibility_scope: Literal["company", "department", "personal"] | None = None
    owner_department_id: str | None = Field(default=None, min_length=1, max_length=64)
    visibility_grants: list[VisibilityGrantRequest] | None = None


class KnowledgeBaseResponse(BaseModel):
    id: str
    name: str
    description: str | None
    visibility_scope: Literal["company", "department", "personal"] = "personal"
    owner_department_id: str | None = None
    visibility_grants: list[VisibilityGrantResponse] = Field(default_factory=list)
    created_at: str
    updated_at: str


class KnowledgeBaseListResponse(BaseModel):
    items: list[KnowledgeBaseResponse]
    page: int
    page_size: int


class KnowledgeTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=1_000_000)


class KnowledgeDocumentPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=255)


class KnowledgeVersionResponse(BaseModel):
    id: str
    document_id: str
    state: str
    error_code: str | None
    warnings: list[str]
    chunk_count: int
    created_at: str
    completed_at: str | None


class KnowledgeDocumentResponse(BaseModel):
    id: str
    knowledge_base_id: str
    title: str
    active_version_id: str | None
    pending_version_id: str | None
    created_at: str
    updated_at: str
    latest_version: KnowledgeVersionResponse | None = None


class KnowledgeDocumentListResponse(BaseModel):
    items: list[KnowledgeDocumentResponse]
    page: int
    page_size: int


class KnowledgeImportResponse(BaseModel):
    document_id: str
    version_id: str
    state: str = "queued"
    status_url: str


class KnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=8, ge=1, le=20)


class KnowledgeSearchHit(BaseModel):
    chunk_id: str
    chunk_ids: list[str] = Field(default_factory=list)
    document_id: str
    version_id: str
    title: str
    text: str
    locators: list[dict[str, object]]
    channels: list[str]


class KnowledgeSearchResponse(BaseModel):
    hits: list[KnowledgeSearchHit]
    channels: list[str]
    degraded_reasons: list[str]
