"""Skill 元数据、不可变版本、Run 快照及安全审批投影。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from common.json import dumps
from domain.access import VisibilityGrant

MAX_PACKAGE_ZIP_BYTES = 25 * 1024 * 1024
MAX_PACKAGE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_PACKAGE_FILE_BYTES = 25 * 1024 * 1024
MAX_PACKAGE_FILES = 500
MAX_GUIDE_CHARACTERS = 100_000
MAX_SKILL_NAME_CHARACTERS = 120
MAX_SKILL_DESCRIPTION_CHARACTERS = 1_000
MAX_PERSONAL_MARKDOWN_BYTES = 1024 * 1024
SKILL_LOAD_TOOL_ID = "builtin_skill_load"
SKILL_READ_RESOURCE_TOOL_ID = "builtin_skill_read_resource"
SKILL_EXECUTE_TOOL_ID = "builtin_skill_execute"
SESSION_FILE_LIST_TOOL_ID = "builtin_session_file_list"
SESSION_FILE_READ_TOOL_ID = "builtin_session_file_read"


class DependencyCheckStatus(StrEnum):
    """导入环境中的依赖检查结论。"""

    SATISFIED = "satisfied"
    UNSATISFIED = "dependency_unsatisfied"
    INVALID = "dependency_invalid"


class SkillScope(StrEnum):
    """Skill 对当前用户的三层可见范围（公司/部门/个人）。"""

    COMPANY = "company"
    DEPARTMENT = "department"
    PERSONAL = "personal"


class _FrozenModel(BaseModel):
    """Skill JSON 内容统一使用严格且不可变的结构。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SkillScriptDefinition(_FrozenModel):
    """一个已登记脚本及其 JSON Schema 子集。"""

    path: str = Field(min_length=1, max_length=512)
    label: str = Field(min_length=1, max_length=160)
    purpose: str = Field(min_length=1, max_length=1000)
    parameters: dict[str, Any]

    @model_validator(mode="after")
    def _validate_contract(self) -> SkillScriptDefinition:
        if (
            not self.path.startswith("scripts/")
            or not self.path.endswith(".py")
            or self.path.startswith("scripts/../")
            or "\\" in self.path
        ):
            raise ValueError("skill_script_path_invalid")
        if self.parameters.get("type", "object") != "object":
            raise ValueError("skill_parameter_schema_invalid")
        try:
            dumps(self.model_dump(mode="json"))
        except (TypeError, ValueError, UnicodeError) as error:
            raise ValueError("skill_script_json_invalid") from error
        return self


class PackageResource(_FrozenModel):
    """不可变技能包内的一个文件清单项。"""

    path: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(ge=0, le=MAX_PACKAGE_FILE_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    text: bool


class SkillPackageManifest(_FrozenModel):
    """从 SKILL.md 元数据和 ZIP 实际内容共同生成的完整清单。"""

    version: Literal[1] = 1
    slug: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    dependencies: tuple[str, ...] = ()
    guide: str = Field(max_length=MAX_GUIDE_CHARACTERS)
    scripts: tuple[SkillScriptDefinition, ...] = ()
    files: tuple[PackageResource, ...] = ()

    @field_validator("name", "description", "guide")
    @classmethod
    def _reject_control_characters(cls, value: str) -> str:
        if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
            raise ValueError("skill_text_invalid")
        return value

    @model_validator(mode="after")
    def _validate_manifest_links(self) -> SkillPackageManifest:
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("skill_package_duplicate_path")
        if "SKILL.md" not in paths:
            raise ValueError("skill_entry_missing")
        script_paths = [item.path for item in self.scripts]
        if len(script_paths) != len(set(script_paths)):
            raise ValueError("skill_script_duplicate")
        if any(path not in set(paths) for path in script_paths):
            raise ValueError("skill_script_missing")
        return self


class DependencyCheckItem(_FrozenModel):
    """一个 PEP 508 需求在目标解释器中的检查结果。"""

    requirement: str
    package: str
    satisfied: bool
    installed_version: str | None = None
    detail_code: str | None = None
    marker_applies: bool = True


class DependencyCheck(_FrozenModel):
    """带解释器指纹的依赖检查记录，不作为永久满足证明。"""

    status: DependencyCheckStatus
    python_version: str
    python_implementation: str
    items: tuple[DependencyCheckItem, ...] = ()
    checked_at: datetime

    @model_validator(mode="after")
    def _validate_status(self) -> DependencyCheck:
        expected = all(item.satisfied for item in self.items)
        if (self.status is DependencyCheckStatus.SATISFIED) != expected:
            raise ValueError("skill_dependency_status_invalid")
        return self


@dataclass(frozen=True, slots=True)
class SkillVersion:
    """不可变 Skill 包版本元数据。"""

    id: str
    skill_id: str
    ordinal: int
    label: str
    manifest: SkillPackageManifest
    object_key: str
    package_sha256: str
    dependency_requirements: tuple[str, ...]
    dependency_check: DependencyCheck
    created_by: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Skill:
    """一个稳定 Skill 身份及其当前可用版本指针。"""

    id: str
    slug: str
    current_version_id: str | None
    enabled: bool
    script_requires_approval: bool
    revision: int
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    name: str
    description: str
    current_version_label: str | None = None
    dependency_check: DependencyCheck | None = None
    scope: SkillScope = SkillScope.COMPANY
    owner_id: str | None = None
    deleted_at: datetime | None = None
    owner_department_id: str | None = None
    visibility_grants: tuple[VisibilityGrant, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillVersionPage:
    """按版本序号倒序排列的一页历史。"""

    items: tuple[SkillVersion, ...]
    total: int


@dataclass(frozen=True, slots=True)
class SkillImportRequestResult:
    """一个导入请求键对应的当前返回详情及原始请求版本。"""

    skill: Skill
    version: SkillVersion
    request_version: SkillVersion | None = None


def _normalize_legacy_skill_scope(value: object) -> object:
    """兼容历史 Run 快照中持久化的 public 范围，归一化为 company 防止反序列化失败。"""
    if isinstance(value, str) and value == "public":
        return SkillScope.COMPANY.value
    return value


class RunSkillEntry(_FrozenModel):
    """Run 创建时固定、执行期间不再读取当前管理目录的一项技能。"""

    skill_id: str
    slug: str
    name: str
    description: str
    version_id: str
    version_label: str
    requires_approval: bool
    scope: SkillScope = SkillScope.COMPANY

    @field_validator("scope", mode="before")
    @classmethod
    def _coerce_legacy_scope(cls, value: object) -> object:
        """升级后兼容历史 runs.skill_snapshot 中的 public 范围值。"""
        return _normalize_legacy_skill_scope(value)


class RunSkillSnapshot(_FrozenModel):
    """新 Run 的完整可用目录和用户显式选择集合。"""

    version: Literal[1] = 1
    skills: tuple[RunSkillEntry, ...] = ()
    selected_skill_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_identity(self) -> RunSkillSnapshot:
        ids = [item.skill_id for item in self.skills]
        selected = self.selected_skill_ids
        if len(ids) != len(set(ids)) or len(selected) != len(set(selected)):
            raise ValueError("run_skill_snapshot_duplicate")
        if not set(selected) <= set(ids):
            raise ValueError("run_skill_selection_invalid")
        return self

    def by_id(self, skill_id: str) -> RunSkillEntry | None:
        """只从 Run 冻结快照解析技能。"""
        return next((item for item in self.skills if item.skill_id == skill_id), None)

    def trusted_subset(self, requested: RunSkillSnapshot) -> RunSkillSnapshot:
        """用父 Run 已授权条目重建子 Run 子集，不信任请求中的版本元数据。"""
        parent_ids = {item.skill_id for item in self.skills}
        requested_ids = {item.skill_id for item in requested.skills}
        if not requested_ids <= parent_ids:
            raise ValueError("run_skill_selection_invalid")
        catalog = tuple(
            item for item in self.skills if item.skill_id in requested_ids
        )
        return RunSkillSnapshot(
            skills=catalog,
            selected_skill_ids=requested.selected_skill_ids,
        )


class ActiveSkillGuide(_FrozenModel):
    """注入不可压缩基础上下文的单个固定版本指南。"""

    skill_id: str
    name: str
    version_id: str
    version_label: str
    guide: str
    scope: SkillScope = SkillScope.COMPANY

    @field_validator("scope", mode="before")
    @classmethod
    def _coerce_legacy_scope(cls, value: object) -> object:
        """兼容历史上下文或快照中的 public 范围值。"""
        return _normalize_legacy_skill_scope(value)


class RunSkillPromptContext(_FrozenModel):
    """主提示词之后按固定顺序注入的目录和活跃指南。"""

    catalog: tuple[RunSkillEntry, ...] = ()
    active: tuple[ActiveSkillGuide, ...] = ()


class SafeSkillInputFile(_FrozenModel):
    """审批投影中由服务端关联的输入文件安全元数据。"""

    file_id: str
    filename: str
    mime_type: str | None = None
    size_bytes: int | None = None
    available: bool = True


class SkillParameterSummaryItem(_FrozenModel):
    """按登记 Schema 白名单生成的一个可读参数项。"""

    name: str
    label: str
    value: str


class SafeSkillApprovalProjection(_FrozenModel):
    """待审批项和详情共用的用户态安全快照。"""

    call_id: str
    skill_id: str
    skill_name: str
    skill_version_id: str
    skill_version_label: str
    script_label: str
    purpose: str
    input_files: tuple[SafeSkillInputFile, ...] = ()
    parameter_summary: tuple[SkillParameterSummaryItem, ...] = ()
    hidden_parameter_count: int = Field(default=0, ge=0)
    can_approve: bool = True
    unavailable_reason: str | None = None


class SkillExecutionStatus(StrEnum):
    """脚本附加记录的独立执行状态，不复制 Run 工具状态机。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class SkillExecutionCompletion(_FrozenModel):
    """随工具结果同事务收敛的脚本审计字段。"""

    execution_id: str = Field(min_length=1, max_length=128)
    status: SkillExecutionStatus
    log_object_key: str | None = Field(default=None, max_length=1024)
    log_truncated: bool = False
    log_error_code: str | None = Field(default=None, max_length=128)
    error_code: str | None = Field(default=None, max_length=128)
    cleanup_error_code: str | None = Field(default=None, max_length=128)
    finished_at: datetime


class SkillExecutionArtifact(_FrozenModel):
    """脚本审计中保存的内部产物元数据，不直接映射为公开 DTO。"""

    file_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=255)
    mime_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    object_key: str = Field(min_length=1, max_length=1024)
    output_index: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class SkillExecutionRecord:
    """与现有工具调用一对一关联的脚本执行附加信息。"""

    id: str
    run_id: str
    session_id: str
    tool_call_record_id: str
    skill_id: str
    skill_version_id: str
    script_path: str
    input_file_ids: tuple[str, ...]
    parameters_sha256: str
    python_version: str
    dependency_check: DependencyCheck
    status: SkillExecutionStatus
    log_object_key: str | None
    log_truncated: bool
    log_error_code: str | None
    error_code: str | None
    cleanup_error_code: str | None
    result_artifacts: tuple[SkillExecutionArtifact, ...]
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class SkillExecutionStart:
    """脚本执行启动结果，明确区分本次是否新建了审计记录。"""

    record: SkillExecutionRecord
    created: bool


class SkillPackageError(ValueError):
    """ZIP、元数据或资源引用不合法。"""

    def __init__(
        self,
        code: str = "skill_package_invalid",
        *,
        member_path: str | None = None,
    ) -> None:
        """保存稳定错误码及可安全展示的包内成员路径。"""
        super().__init__(code)
        self.code = code
        self.member_path = member_path


class SkillNotFoundError(LookupError):
    """管理员目标 Skill 或版本不存在。"""

    def __init__(self, code: str = "skill_not_found") -> None:
        """保存安全错误码。"""
        super().__init__(code)
        self.code = code


class SkillConflictError(RuntimeError):
    """slug、修订号或并发状态冲突。"""

    def __init__(self, code: str = "skill_conflict") -> None:
        """保存稳定冲突码。"""
        super().__init__(code)
        self.code = code


class SkillDependencyUnsatisfiedError(ValueError):
    """开启或执行时依赖不再满足。"""

    def __init__(self, code: str = "skill_dependency_unsatisfied") -> None:
        """保存依赖错误的稳定码。"""
        super().__init__(code)
        self.code = code


__all__ = [
    "MAX_GUIDE_CHARACTERS",
    "MAX_PACKAGE_FILES",
    "MAX_PACKAGE_FILE_BYTES",
    "MAX_PACKAGE_UNCOMPRESSED_BYTES",
    "MAX_PACKAGE_ZIP_BYTES",
    "SESSION_FILE_LIST_TOOL_ID",
    "SESSION_FILE_READ_TOOL_ID",
    "SKILL_EXECUTE_TOOL_ID",
    "SKILL_LOAD_TOOL_ID",
    "SKILL_READ_RESOURCE_TOOL_ID",
    "DependencyCheck",
    "DependencyCheckItem",
    "DependencyCheckStatus",
    "ActiveSkillGuide",
    "PackageResource",
    "RunSkillEntry",
    "RunSkillPromptContext",
    "RunSkillSnapshot",
    "SafeSkillApprovalProjection",
    "SafeSkillInputFile",
    "Skill",
    "SkillConflictError",
    "SkillDependencyUnsatisfiedError",
    "SkillExecutionArtifact",
    "SkillExecutionRecord",
    "SkillExecutionCompletion",
    "SkillExecutionStart",
    "SkillExecutionStatus",
    "SkillNotFoundError",
    "SkillPackageError",
    "SkillPackageManifest",
    "SkillParameterSummaryItem",
    "SkillScriptDefinition",
    "SkillVersion",
    "SkillVersionPage",
]
