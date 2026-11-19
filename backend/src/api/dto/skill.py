"""Skill 管理、公开目录与执行排查 HTTP DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SkillDependencyItemResponse(BaseModel):
    requirement: str
    package: str
    satisfied: bool
    installed_version: str | None
    detail_code: str | None
    marker_applies: bool


class SkillDependencyCheckResponse(BaseModel):
    status: str
    python_version: str
    python_implementation: str
    checked_at: datetime
    items: list[SkillDependencyItemResponse]


class SkillPackageResourceResponse(BaseModel):
    path: str
    size_bytes: int
    sha256: str
    text: bool


class SkillScriptResponse(BaseModel):
    path: str
    label: str
    purpose: str
    parameters: dict[str, object]


class SkillVersionResponse(BaseModel):
    id: str
    skill_id: str
    ordinal: int
    label: str
    slug: str
    name: str
    description: str
    guide: str
    dependencies: list[str]
    dependency_check: SkillDependencyCheckResponse
    scripts: list[SkillScriptResponse]
    files: list[SkillPackageResourceResponse]
    package_sha256: str
    created_at: datetime


class SkillSummaryResponse(BaseModel):
    id: str
    slug: str
    scope: Literal["public", "personal"]
    name: str
    description: str
    current_version_id: str | None
    current_version_label: str | None
    enabled: bool
    script_requires_approval: bool
    revision: int
    updated_at: datetime
    dependency_check: SkillDependencyCheckResponse | None = None


class SkillAdminDetailsResponse(SkillSummaryResponse):
    versions: list[SkillVersionResponse]


class SkillAdminListResponse(BaseModel):
    items: list[SkillSummaryResponse]


class SkillDirectoryResponse(BaseModel):
    items: list[SkillSummaryResponse]


class SkillPersonalListResponse(BaseModel):
    items: list[SkillSummaryResponse]


class SkillVersionListResponse(BaseModel):
    items: list[SkillVersionResponse]
    total: int
    page: int
    page_size: int


class SkillConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    current_version_id: str = Field(min_length=1, max_length=128)
    enabled: bool
    script_requires_approval: bool


class SkillPersonalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    guide: str


class SkillPersonalVersionRequest(SkillPersonalCreateRequest):
    expected_revision: int = Field(ge=1)


class SkillPersonalToggleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    expected_revision: int = Field(ge=1)


class SkillMarkdownParseResponse(BaseModel):
    name: str
    description: str
    guide: str


class SkillExecutionResponse(BaseModel):
    id: str
    run_id: str
    session_id: str
    call_id: str
    skill_id: str
    skill_version_id: str
    script_path: str
    input_file_ids: list[str]
    python_version: str
    dependency_check: SkillDependencyCheckResponse
    status: str
    log_available: bool
    log_truncated: bool
    log_error_code: str | None
    error_code: str | None
    cleanup_error_code: str | None
    created_at: datetime
    finished_at: datetime | None


__all__ = [
    "SkillAdminDetailsResponse",
    "SkillAdminListResponse",
    "SkillConfigurationRequest",
    "SkillDependencyCheckResponse",
    "SkillDependencyItemResponse",
    "SkillDirectoryResponse",
    "SkillExecutionResponse",
    "SkillMarkdownParseResponse",
    "SkillPackageResourceResponse",
    "SkillPersonalCreateRequest",
    "SkillPersonalListResponse",
    "SkillPersonalToggleRequest",
    "SkillPersonalVersionRequest",
    "SkillScriptResponse",
    "SkillSummaryResponse",
    "SkillVersionListResponse",
    "SkillVersionResponse",
]
