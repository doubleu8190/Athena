"""Unified Run tool catalog, authorization, and single-call dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from functools import lru_cache
from typing import Any, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from app.access import AccessPolicyService
from app.file.service import FileInputError, SessionFileService
from app.knowledge.service import (
    KnowledgeInputError,
    KnowledgeNotFoundError,
    KnowledgeSearchUnavailableError,
    KnowledgeService,
)
from app.preference import PreferenceService
from app.project.knowledge import KnowledgeNotFoundError as ProjectKnowledgeNotFoundError
from app.project.knowledge import ProjectKnowledgeService
from app.skill.errors import public_skill_error_code
from app.skill.execution_service import SkillExecutionService
from app.skill.service import SkillService
from app.skill.tooling import summarize_parameters
from app.tool.ports import RunToolRuntime
from common.date import utc_now
from common.json import utf8_size
from domain.access import ResourceType, VisibilityScope
from domain.auth.ports import CurrentSessionReader
from domain.file import PendingSessionFile, SessionFile, TextFileReadError
from domain.knowledge.model import SearchResult
from domain.mcp import (
    McpCallResult,
    McpCallStatus,
    McpToolExecutor,
    McpToolRuntimeReader,
)
from domain.preference import (
    MAX_USER_MEMORY_CONTENT_LENGTH,
    MIN_USER_MEMORY_CONTENT_LENGTH,
    SAVE_USER_MEMORY_TOOL_ID,
    InvalidUserMemoryContentError,
    UserMemoryDisabledError,
    UserMemoryLimitReachedError,
    UserMemorySource,
)
from domain.project import ProjectNotFoundError
from domain.run import Run, RunInputFileContext
from domain.session import SessionRepository, SessionScope
from domain.skill import (
    SESSION_FILE_LIST_TOOL_ID,
    SESSION_FILE_READ_TOOL_ID,
    SKILL_EXECUTE_TOOL_ID,
    SKILL_LOAD_TOOL_ID,
    SKILL_READ_RESOURCE_TOOL_ID,
    RunSkillPromptContext,
    RunSkillSnapshot,
    SafeSkillApprovalProjection,
    SafeSkillInputFile,
    SkillDependencyUnsatisfiedError,
    SkillExecutionStatus,
    SkillNotFoundError,
    SkillPackageError,
)
from domain.subagent import DISPATCH_SUBAGENTS_TOOL_ID, SubagentProfile, SubagentRepository
from domain.tool import (
    MAX_TOOL_ARGUMENT_BYTES,
    AuthorizedRunContext,
    BuiltinToolSource,
    McpToolSource,
    RuntimeToolDefinition,
    RuntimeToolSnapshot,
    ToolCallPreparation,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolOutcome,
    ToolOutcomeStatus,
    bounded_outcome,
)

REQUEST_USER_INPUT_ID = "builtin_request_user_input"
KNOWLEDGE_SEARCH_ID = "builtin_knowledge_search"
MAX_KNOWLEDGE_SEARCH_BASES = 20
BUILTIN_VERSION = 1
MCP_DELIVERY_AUTH_TIMEOUT_SECONDS = 1.0
MCP_TOOL_WATCHDOG_GRACE_SECONDS = 0.25
MCP_DELIVERY_AUTH_ERROR = "run_auth_session_invalid"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _KnowledgeScopeResolution:
    """统一知识库范围解析结果：effective_ids 为实际待检集合，allowed_ids 为交付复核全集。"""

    effective_ids: tuple[str, ...]
    allowed_ids: frozenset[str]
    error_code: str | None = None
    error_message: str | None = None


REQUEST_USER_INPUT_DEFINITION = RuntimeToolDefinition(
    tool_id=REQUEST_USER_INPUT_ID,
    model_name="request_user_input",
    description="当缺少继续处理所需的关键信息时，向用户提出一个问题。",
    input_schema={
        "type": "object",
        "properties": {"question": {"type": "string", "minLength": 1}},
        "required": ["question"],
        "additionalProperties": False,
    },
    source=BuiltinToolSource(version=BUILTIN_VERSION),
    requires_approval=False,
)

SAVE_USER_MEMORY_DEFINITION = RuntimeToolDefinition(
    tool_id=SAVE_USER_MEMORY_TOOL_ID,
    model_name="save_user_memory",
    description=(
        "当用户在对话中明确要求你记住某条长期工作偏好、背景事实或常用约定时调用，"
        "将其保存为用户的显式备忘记忆（1-500 字符）。仅保存用户明确要求记住的精炼信息，"
        "不要未经用户同意擅自记录敏感信息或临时任务细节。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "minLength": MIN_USER_MEMORY_CONTENT_LENGTH,
                "maxLength": MAX_USER_MEMORY_CONTENT_LENGTH,
            }
        },
        "required": ["content"],
        "additionalProperties": False,
    },
    source=BuiltinToolSource(version=BUILTIN_VERSION),
    requires_approval=False,
)

KNOWLEDGE_SEARCH_DEFINITION = RuntimeToolDefinition(
    tool_id=KNOWLEDGE_SEARCH_ID,
    model_name="knowledge_search",
    description=(
        "检索当前对话范围内且当前用户仍有权访问的知识库（含项目关联库及公司/部门共享知识库）。"
        "只返回安全的文档标题、片段和来源标识；零命中与检索不可用必须区分。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 4000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    source=BuiltinToolSource(version=BUILTIN_VERSION),
    requires_approval=False,
)

SKILL_LOAD_DEFINITION = RuntimeToolDefinition(
    tool_id=SKILL_LOAD_TOOL_ID,
    model_name="skill_load",
    description=(
        "加载当前 Run 固定目录中的一个 Skill。成功后的完整指南会进入下一轮基础上下文；"
        "返回资源与登记脚本清单，不执行脚本。"
    ),
    input_schema={
        "type": "object",
        "properties": {"skill_id": {"type": "string", "minLength": 1, "maxLength": 128}},
        "required": ["skill_id"],
        "additionalProperties": False,
    },
    source=BuiltinToolSource(version=BUILTIN_VERSION),
    requires_approval=False,
)

SKILL_READ_RESOURCE_DEFINITION = RuntimeToolDefinition(
    tool_id=SKILL_READ_RESOURCE_TOOL_ID,
    model_name="skill_read_resource",
    description="按当前 Run 固定 Skill 版本分段读取一个文本资源。",
    input_schema={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "path": {"type": "string", "minLength": 1, "maxLength": 512},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 65536,
                "default": 65536,
            },
        },
        "required": ["skill_id", "path"],
        "additionalProperties": False,
    },
    source=BuiltinToolSource(version=BUILTIN_VERSION),
    requires_approval=False,
)

SKILL_EXECUTE_DEFINITION = RuntimeToolDefinition(
    tool_id=SKILL_EXECUTE_TOOL_ID,
    model_name="skill_execute",
    description=(
        "执行当前 Run 固定 Skill 中登记的 Python 脚本。必须显式提供结构化参数和当前会话"
        "输入文件；是否要求用户审批由 Run 创建时冻结的 Skill 策略决定。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "script_path": {"type": "string", "minLength": 1, "maxLength": 512},
            "parameters": {"type": "object"},
            "input_file_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "maxItems": 100,
                "uniqueItems": True,
                "default": [],
            },
        },
        "required": ["skill_id", "script_path", "parameters"],
        "additionalProperties": False,
    },
    source=BuiltinToolSource(version=BUILTIN_VERSION),
    requires_approval=False,
)

SESSION_FILE_LIST_DEFINITION = RuntimeToolDefinition(
    tool_id=SESSION_FILE_LIST_TOOL_ID,
    model_name="session_file_list",
    description="按稳定顺序列出当前会话中可引用的上传文件和产物。",
    input_schema={
        "type": "object",
        "properties": {
            "after_file_id": {"type": ["string", "null"], "maxLength": 128},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
        },
        "additionalProperties": False,
    },
    source=BuiltinToolSource(version=BUILTIN_VERSION),
    requires_approval=False,
)

SESSION_FILE_READ_DEFINITION = RuntimeToolDefinition(
    tool_id=SESSION_FILE_READ_TOOL_ID,
    model_name="session_file_read",
    description="严格按 UTF-8 字符边界分段读取当前会话白名单文本文件。",
    input_schema={
        "type": "object",
        "properties": {
            "file_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 65536,
                "default": 65536,
            },
        },
        "required": ["file_id"],
        "additionalProperties": False,
    },
    source=BuiltinToolSource(version=BUILTIN_VERSION),
    requires_approval=False,
)


_DISPATCH_SUBAGENTS_DESCRIPTION_MAX_CHARS = 8192
_DISPATCH_SUBAGENT_ITEM_DESCRIPTION_MAX_CHARS = 240
_DISPATCH_SUBAGENTS_DESCRIPTION_PREFIX = (
    "将 1 到 5 个独立子任务异步派发给专业子智能体并发执行。"
    "派发后当前轮次将暂停等待全部子智能体完成并自动汇总结果。\n可用子智能体：\n"
)


def _truncate_catalog_text(value: str, maximum_chars: int) -> str:
    """按字符预算裁剪单条目录描述，避免单个 Subagent 占满工具描述空间。"""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) <= maximum_chars:
        return normalized
    if maximum_chars <= 1:
        return "…"[:maximum_chars]
    return normalized[: maximum_chars - 1].rstrip() + "…"


def build_dispatch_subagents_definition(
    subagents: Sequence[SubagentProfile],
) -> RuntimeToolDefinition:
    """按当前已启用 Subagent 列表构造主 Run 专用的异步派发工具定义。"""
    # 严格控制最终描述不超过 RuntimeToolDefinition.description 的 8192 字符上限，
    # 避免启用多个长描述 Subagent 时主 Run 冻结工具快照失败。
    lines: list[str] = []
    current_length = len(_DISPATCH_SUBAGENTS_DESCRIPTION_PREFIX)
    total = len(subagents)
    for index, item in enumerate(subagents):
        trimmed_desc = _truncate_catalog_text(
            item.description,
            _DISPATCH_SUBAGENT_ITEM_DESCRIPTION_MAX_CHARS,
        )
        line = f"- {item.slug}（{item.name}）：{trimmed_desc}"
        remaining_after = total - index - 1
        separator_len = 1 if lines else 0
        reserved_suffix = (
            f"\n…其余 {remaining_after} 个子智能体已省略" if remaining_after > 0 else ""
        )
        if (
            current_length + separator_len + len(line) + len(reserved_suffix)
            <= _DISPATCH_SUBAGENTS_DESCRIPTION_MAX_CHARS
        ):
            lines.append(line)
            current_length += separator_len + len(line)
        else:
            omitted_count = total - index
            omitted_line = f"…其余 {omitted_count} 个子智能体已省略"
            if (
                current_length + separator_len + len(omitted_line)
                <= _DISPATCH_SUBAGENTS_DESCRIPTION_MAX_CHARS
            ):
                lines.append(omitted_line)
            break
    description = (_DISPATCH_SUBAGENTS_DESCRIPTION_PREFIX + "\n".join(lines))[
        :_DISPATCH_SUBAGENTS_DESCRIPTION_MAX_CHARS
    ]
    return RuntimeToolDefinition(
        tool_id=DISPATCH_SUBAGENTS_TOOL_ID,
        model_name="dispatch_subagents",
        description=description,
        input_schema={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "subagent_slug": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 64,
                            },
                            "task": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 32000,
                            },
                            "context": {
                                "type": "string",
                                "maxLength": 32000,
                            },
                            "input_file_ids": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 128,
                                },
                                "maxItems": 100,
                                "uniqueItems": True,
                                "default": [],
                            },
                        },
                        "required": ["subagent_slug", "task"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["tasks"],
            "additionalProperties": False,
        },
        source=BuiltinToolSource(version=BUILTIN_VERSION),
        requires_approval=False,
    )


class RunToolRuntimeService(RunToolRuntime):
    """Build a frozen catalog and execute one validated tool without retries."""

    def __init__(
        self,
        *,
        auth: CurrentSessionReader,
        sessions: SessionRepository,
        mcp_catalog: McpToolRuntimeReader,
        mcp_executor: McpToolExecutor,
        knowledge: KnowledgeService | None,
        project_knowledge: ProjectKnowledgeService | None,
        preferences: PreferenceService | None = None,
        skills: SkillService | None = None,
        session_files: SessionFileService | None = None,
        skill_execution: SkillExecutionService | None = None,
        subagents: SubagentRepository | None = None,
        access_policy: AccessPolicyService | None = None,
        clock: Any = utc_now,
    ) -> None:
        self._auth = auth
        self._sessions = sessions
        self._mcp_catalog = mcp_catalog
        self._mcp_executor = mcp_executor
        self._knowledge = knowledge
        self._project_knowledge = project_knowledge
        self._preferences = preferences
        self._skills = skills
        self._session_files = session_files
        self._skill_execution = skill_execution
        self._subagents = subagents
        self._access_policy = access_policy
        self._clock = clock

    async def can_read_subagent(self, user_id: str, subagent_id: str) -> bool:
        """供 RunExecutor 调度子智能体前复核当前用户对目标 Subagent 的实时读权限。"""
        if self._access_policy is None:
            return True
        return await self._access_policy.can_read_resource(
            user_id,
            ResourceType.SUBAGENT,
            subagent_id,
        )

    async def authorize(
        self,
        owner_id: str,
        chat_session_id: str,
        run_id: str,
        auth_session_id: str | None,
    ) -> AuthorizedRunContext | None:
        del run_id
        # 未绑定登录会话时不授权任何工具能力。
        if not auth_session_id:
            return None
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)
        # 登录会话必须有效且属于当前 owner，否则视为未授权。
        user = await self._auth.get_current_session_user(owner_id, auth_session_id, now)
        if user is None or user.id != owner_id:
            return None
        # 聊天会话必须对 owner 可见（未删除），项目归属用于项目级工具。
        session = await self._sessions.get_visible(
            SessionScope(session_id=chat_session_id, owner_id=owner_id)
        )
        if session is None:
            return None
        return AuthorizedRunContext(
            owner_id=owner_id,
            chat_session_id=chat_session_id,
            project_id=session.project_id,
            auth_session_id=auth_session_id,
        )

    async def _resolve_knowledge_scope(
        self,
        owner_id: str,
        project_id: str | None,
        knowledge_base_ids: tuple[str, ...] | None,
        *,
        strict_project_limit: bool,
        ignore_missing_project: bool = False,
    ) -> _KnowledgeScopeResolution:
        """统一解析本次对话的有效知识库集合，供 build_snapshot 与 _knowledge_search 共用。

        模式说明：
        1. 显式模式（knowledge_base_ids is not None）：
           - 空元组表示显式关闭知识库检索；
           - 非空列表经 AccessPolicyService 实时过滤为当前仍未删除且可读的集合（上限 20）。
        2. 默认企业工作台模式（knowledge_base_ids is None）：
           - 计算并集：当前项目关联的可读知识库（若 project_id 非空）
             ∪ 当前用户有权访问的 company 与 department 共享知识库（不自动混入未绑项目的个人库）；
           - 按确定优先级去重排序（项目绑定库 > 部门库 > 公司库），超过 20 个时截取前 20 个。
        """
        if self._knowledge is None:
            return _KnowledgeScopeResolution(
                (),
                frozenset(),
                "knowledge_unavailable",
                "当前部署未启用知识库检索能力。",
            )

        if knowledge_base_ids is not None:
            ordered_ids = tuple(dict.fromkeys(knowledge_base_ids))
            if not ordered_ids:
                return _KnowledgeScopeResolution(
                    (),
                    frozenset(),
                    "knowledge_unavailable",
                    "当前对话已关闭知识库检索。",
                )
            if len(ordered_ids) > MAX_KNOWLEDGE_SEARCH_BASES:
                return _KnowledgeScopeResolution(
                    (),
                    frozenset(),
                    "knowledge_scope_unavailable",
                    "指定的知识库数量超过单次检索上限。",
                )
            if self._access_policy is not None:
                readable_ids = await self._access_policy.filter_readable_ids(
                    owner_id,
                    ResourceType.KNOWLEDGE_BASE,
                    ordered_ids,
                )
                effective = tuple(kb_id for kb_id in ordered_ids if kb_id in readable_ids)
            else:
                get_base = getattr(self._knowledge, "get_base", None)
                if callable(get_base):
                    filtered: list[str] = []
                    for kb_id in ordered_ids:
                        try:
                            await get_base(owner_id, kb_id)
                        except KnowledgeNotFoundError:
                            continue
                        filtered.append(kb_id)
                    effective = tuple(filtered)
                else:
                    effective = ordered_ids
            if not effective:
                return _KnowledgeScopeResolution(
                    (),
                    frozenset(),
                    "knowledge_scope_unavailable",
                    "指定的知识库已不可用或当前用户已失去访问权限。",
                )
            return _KnowledgeScopeResolution(effective, frozenset(effective))

        # 默认企业工作台模式：先收集项目绑定库，再合并企业共享库（部门库 > 公司库）。
        project_ids: list[str] = []
        if project_id is not None and self._project_knowledge is not None:
            try:
                project_bases = await self._project_knowledge.list(owner_id, project_id)
            except (ProjectNotFoundError, ProjectKnowledgeNotFoundError):
                if not ignore_missing_project:
                    raise
                project_bases = []
            if strict_project_limit and len(project_bases) > MAX_KNOWLEDGE_SEARCH_BASES:
                return _KnowledgeScopeResolution(
                    (),
                    frozenset(),
                    "knowledge_scope_unavailable",
                    "项目关联的知识库超过单次检索上限。",
                )
            project_ids = [base.id for base in project_bases]

        dept_ids: list[str] = []
        company_ids: list[str] = []
        if self._access_policy is not None:
            readable_targets = await self._access_policy.filter_readable_targets(
                owner_id,
                ResourceType.KNOWLEDGE_BASE,
            )
            # 仅自动纳入 company 与 department 共享知识库，未绑定项目的个人库不自动混入。
            dept_ids = sorted(
                target.resource_id
                for target in readable_targets
                if target.visibility_scope is VisibilityScope.DEPARTMENT
            )
            company_ids = sorted(
                target.resource_id
                for target in readable_targets
                if target.visibility_scope is VisibilityScope.COMPANY
            )

        all_ordered = tuple(dict.fromkeys((*project_ids, *dept_ids, *company_ids)))
        if not all_ordered:
            return _KnowledgeScopeResolution(
                (),
                frozenset(),
                "knowledge_unavailable",
                "当前对话没有可查询的知识库。",
            )
        return _KnowledgeScopeResolution(
            all_ordered[:MAX_KNOWLEDGE_SEARCH_BASES],
            frozenset(all_ordered),
        )

    async def build_snapshot(
        self,
        prompt_template_version: str,
        context: AuthorizedRunContext,
        *,
        depth: int = 0,
        allowed_tool_ids: Sequence[str] | None = None,
        knowledge_base_ids: tuple[str, ...] | None = None,
    ) -> RuntimeToolSnapshot:
        """Freeze the complete v2 tool catalog."""
        # 只支持当前模板版本；其余版本（含历史 v1）统一拒绝。
        if prompt_template_version != "run-v2":
            raise ValueError("run_prompt_template_unsupported")

        definitions: list[RuntimeToolDefinition] = [REQUEST_USER_INPUT_DEFINITION]
        # 仅主 Run 且用户开启显式记忆开关时，暴露免审批的 save_user_memory 工具。
        if depth == 0 and self._preferences is not None:
            if await self._preferences.is_memory_enabled(context.owner_id):
                definitions.append(SAVE_USER_MEMORY_DEFINITION)
        # 统一解析有效知识库范围：无项目对话只要存在可读共享库或显式指定库，
        # 同样自动暴露 knowledge_search。
        if self._knowledge is not None:
            try:
                scope = await self._resolve_knowledge_scope(
                    context.owner_id,
                    context.project_id,
                    knowledge_base_ids,
                    strict_project_limit=False,
                    ignore_missing_project=True,
                )
            except Exception:
                scope = _KnowledgeScopeResolution((), frozenset())
            if scope.effective_ids:
                definitions.append(KNOWLEDGE_SEARCH_DEFINITION)
        # Skill 与文件工具由独立能力开关启用，不依赖知识库或嵌入模型。
        if self._skills is not None:
            definitions.extend((SKILL_LOAD_DEFINITION, SKILL_READ_RESOURCE_DEFINITION))
        if self._skill_execution is not None:
            definitions.append(SKILL_EXECUTE_DEFINITION)
        if self._session_files is not None:
            definitions.extend(
                (
                    SESSION_FILE_LIST_DEFINITION,
                    SESSION_FILE_READ_DEFINITION,
                )
            )
        # 合并 MCP 目录中的工具定义，并按统一三层权限引擎实时过滤掉无权访问的部门 MCP 工具。
        mcp_tools = await self._mcp_catalog.list_runtime_tools(limit=201)
        if self._access_policy is not None and mcp_tools:
            server_ids = tuple(
                dict.fromkeys(cast(McpToolSource, item.source).server_id for item in mcp_tools)
            )
            readable_server_ids = await self._access_policy.filter_readable_ids(
                context.owner_id,
                ResourceType.MCP_SERVER,
                server_ids,
            )
            mcp_tools = tuple(
                item
                for item in mcp_tools
                if cast(McpToolSource, item.source).server_id in readable_server_ids
            )
        definitions.extend(mcp_tools)

        if depth == 0 and self._subagents is not None:
            enabled_subagents = await self._subagents.list_enabled()
            if self._access_policy is not None and enabled_subagents:
                readable_subagent_ids = await self._access_policy.filter_readable_ids(
                    context.owner_id,
                    ResourceType.SUBAGENT,
                    [item.id for item in enabled_subagents],
                )
                enabled_subagents = tuple(
                    item for item in enabled_subagents if item.id in readable_subagent_ids
                )
            if enabled_subagents:
                definitions.append(build_dispatch_subagents_definition(enabled_subagents))
        elif depth >= 1:
            # 子 Run 强制剔除 dispatch_subagents 与 save_user_memory，
            # 并与子智能体白名单求严格交集（白名单 ∩ 当前员工实时有权限的 MCP 工具）。
            forbidden_tool_ids = {DISPATCH_SUBAGENTS_TOOL_ID, SAVE_USER_MEMORY_TOOL_ID}
            forbidden_names = {"dispatch_subagents", "save_user_memory"}
            definitions = [
                item
                for item in definitions
                if item.tool_id not in forbidden_tool_ids and item.model_name not in forbidden_names
            ]
            if allowed_tool_ids is not None:
                allowed_set = set(allowed_tool_ids) - forbidden_tool_ids
                definitions = [item for item in definitions if item.tool_id in allowed_set]

        return RuntimeToolSnapshot(tools=tuple(definitions))

    async def prepare_call(
        self,
        definition: RuntimeToolDefinition | None,
        model_name: str,
        arguments: dict[str, Any],
        run: Run,
        context: AuthorizedRunContext | None,
        *,
        call_id: str,
    ) -> ToolCallPreparation:
        """在审批屏障前校验参数、固定版本、依赖和输入文件权限。"""
        if context is None:
            return ToolCallPreparation(
                _error("tool_permission_denied", "当前登录或聊天权限已失效。"),
                False,
            )
        if (
            definition is None
            or definition.source.kind != "builtin"
            or definition.tool_id != SKILL_EXECUTE_TOOL_ID
        ):
            validation = self.validate_call(definition, model_name, arguments)
            if validation is None and definition is not None and definition.source.kind == "mcp":
                if self._access_policy is not None:
                    # 审批/执行准备阶段实时复核 MCP 读权限，防员工调岗后仍进入审批或执行。
                    if not await self._access_policy.can_read_resource(
                        context.owner_id,
                        ResourceType.MCP_SERVER,
                        definition.source.server_id,
                    ):
                        return ToolCallPreparation(
                            _error("tool_permission_denied", "当前用户无权使用该 MCP 工具。"),
                            False,
                        )
                # 同步复核 MCP Server 是否仍处于启用且版本匹配状态；停用、软删或变更后即刻拦截。
                target = await self._mcp_catalog.get_call_target(
                    definition.tool_id,
                    definition.source.server_id,
                    definition.source.remote_name,
                    definition.source.config_version,
                )
                if target is None:
                    return ToolCallPreparation(
                        _error("tool_unavailable", "工具配置已变更或工具已不可用。"),
                        False,
                    )
            return ToolCallPreparation(
                outcome=validation,
                requires_approval=(
                    False
                    if validation is not None or definition is None
                    else definition.requires_approval
                ),
            )
        validation = self.validate_call(definition, model_name, arguments)
        if validation is not None:
            return ToolCallPreparation(validation, False)
        if self._skills is None or self._session_files is None:
            return ToolCallPreparation(
                _error("tool_unavailable", "当前部署未启用 Skill/文件能力。"),
                False,
            )
        snapshot = run.skill_snapshot
        if snapshot is None:
            return ToolCallPreparation(
                _error("skill_snapshot_unavailable", "当前 Run 没有固定 Skill 目录。"),
                False,
            )
        skill_id = arguments.get("skill_id")
        script_path = arguments.get("script_path")
        parameters = arguments.get("parameters")
        input_file_ids = arguments.get("input_file_ids", [])
        if (
            not isinstance(skill_id, str)
            or not isinstance(script_path, str)
            or not isinstance(parameters, dict)
            or not isinstance(input_file_ids, list)
            or any(not isinstance(item, str) for item in input_file_ids)
        ):
            return ToolCallPreparation(
                _error("skill_parameters_invalid", "脚本调用参数结构不合法。"),
                False,
            )
        if self._access_policy is not None:
            # 脚本审批准备阶段实时复核用户是否仍对该 Skill 拥有读权限（防调岗后沿用旧快照）。
            if not await self._access_policy.can_read_resource(
                context.owner_id,
                ResourceType.SKILL,
                skill_id,
            ):
                return ToolCallPreparation(
                    _error("tool_permission_denied", "当前用户已失去该 Skill 的访问权限。"),
                    False,
                )
        try:
            entry, version, script, _dependency_check = await self._skills.prepare_script_target(
                snapshot,
                skill_id=skill_id,
                script_path=script_path,
                parameters=cast(dict[str, Any], parameters),
            )
            files = await self._session_files.get_many(
                context.chat_session_id,
                context.owner_id,
                tuple(cast(list[str], input_file_ids)),
            )
        except (
            FileInputError,
            SkillDependencyUnsatisfiedError,
            SkillNotFoundError,
            SkillPackageError,
            ValueError,
        ) as error:
            return ToolCallPreparation(_skill_preparation_error(error), False)
        summary, hidden_count = summarize_parameters(script, parameters)
        projection = SafeSkillApprovalProjection(
            call_id=call_id,
            skill_id=entry.skill_id,
            skill_name=entry.name,
            skill_version_id=version.id,
            skill_version_label=entry.version_label,
            script_label=script.label,
            purpose=script.purpose,
            input_files=tuple(
                SafeSkillInputFile(
                    file_id=file.id,
                    filename=file.filename,
                    mime_type=file.mime_type,
                    size_bytes=file.size_bytes,
                    available=True,
                )
                for file in files
            ),
            parameter_summary=summary,
            hidden_parameter_count=hidden_count,
            can_approve=True,
        )
        return ToolCallPreparation(
            outcome=None,
            requires_approval=entry.requires_approval,
            safe_skill_projection=projection,
        )

    async def build_skill_context(
        self,
        run: Run,
        loaded_skill_ids: tuple[str, ...],
    ) -> RunSkillPromptContext:
        """从 Run 固定快照和持久化成功加载证据重建指南，并按实时权限剔除已失权技能。"""
        # 空固定目录表示 Run 未启用 Skill，不要求装配 SkillService。
        if run.skill_snapshot is None or not run.skill_snapshot.skills:
            return RunSkillPromptContext()
        if self._skills is None:
            raise ValueError("skill_runtime_unavailable")
        effective_snapshot = run.skill_snapshot
        if self._access_policy is not None:
            readable_ids = await self._access_policy.filter_readable_ids(
                run.owner_id,
                ResourceType.SKILL,
                [item.skill_id for item in effective_snapshot.skills],
            )
            filtered_skills = tuple(
                item for item in effective_snapshot.skills if item.skill_id in readable_ids
            )
            if not filtered_skills:
                return RunSkillPromptContext()
            effective_snapshot = RunSkillSnapshot(
                skills=filtered_skills,
                selected_skill_ids=tuple(
                    sid for sid in effective_snapshot.selected_skill_ids if sid in readable_ids
                ),
            )
        selected = set(effective_snapshot.selected_skill_ids)
        loaded = set(loaded_skill_ids)
        active_ids = tuple(
            item.skill_id
            for item in effective_snapshot.skills
            if item.skill_id in selected or item.skill_id in loaded
        )
        return await self._skills.prompt_context(effective_snapshot, active_ids)

    async def build_run_input_file_context(
        self,
        run: Run,
    ) -> tuple[RunInputFileContext, ...]:
        """解析 Run 累计绑定文件；任何失效或跨会话引用都阻止模型执行。"""
        file_ids = run.input_files.file_ids
        if not file_ids:
            return ()
        if self._session_files is None:
            raise FileInputError("session_file_runtime_unavailable")
        files = await self._session_files.get_many(
            run.session_id,
            run.owner_id,
            file_ids,
        )
        return tuple(
            RunInputFileContext(
                file_id=file.id,
                filename=file.filename,
                label=file.label,
                mime_type=file.mime_type,
                size_bytes=file.size_bytes,
                source=file.source.value,
            )
            for file in files
        )

    def validate_call(
        self,
        definition: RuntimeToolDefinition | None,
        model_name: str,
        arguments: dict[str, Any],
    ) -> ToolOutcome | None:
        """Validate frozen identity and local JSON Schema without network access."""
        # 本地校验不触网：先看参数体积，再确认工具仍在冻结目录中。
        if arguments and utf8_size(arguments) > MAX_TOOL_ARGUMENT_BYTES:
            return _error("tool_arguments_too_large", "工具参数超过上限。")
        if definition is None or definition.model_name != model_name:
            return _error("tool_unknown", "模型请求了冻结目录中不存在的工具。")
        if definition.source.kind == "builtin" and (
            definition.source.version != _current_builtin_version(definition)
        ):
            return _error(
                "tool_version_unavailable",
                "内置工具版本已更新，当前冻结调用不会执行。",
            )
        # 按冻结的 JSON Schema 校验参数形状。
        validation_error = _validate_arguments(definition.input_schema, arguments)
        if validation_error is not None:
            return _error("tool_arguments_invalid", validation_error)
        query = arguments.get("query")
        if (
            definition.model_name == "knowledge_search"
            and isinstance(query, str)
            and not query.strip()
        ):
            return _error("tool_arguments_invalid", "查询内容不能为空。")
        content = arguments.get("content")
        if (
            definition.tool_id == SAVE_USER_MEMORY_TOOL_ID
            and isinstance(content, str)
            and not content.strip()
        ):
            return _error("tool_arguments_invalid", "记忆内容不能为空。")
        return None

    async def abandon_artifacts(
        self,
        artifacts: tuple[PendingSessionFile, ...],
    ) -> None:
        """在工具结果落库失败时主动清理已上传但未发布的临时产物对象。"""
        if not artifacts or self._session_files is None:
            return
        await self._session_files.abandon_artifacts(artifacts)

    async def execute(
        self,
        definition: RuntimeToolDefinition | None,
        model_name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        *,
        deadline_seconds: float,
        run_remaining_seconds: float,
    ) -> ToolExecutionResult:
        """Validate locally, dispatch once, then re-authorize before delivery."""
        # 执行前再校验一次，确保入口调用方无法绕过本地约束。
        validation_error = self.validate_call(definition, model_name, arguments)
        if validation_error is not None:
            return ToolExecutionResult(validation_error)
        assert definition is not None
        # 按来源分派：MCP 有独立的三段超时收敛，内置工具直接执行。
        if definition.source.kind == "mcp":
            return await self._execute_mcp(
                definition,
                arguments,
                context,
                deadline_seconds,
                run_remaining_seconds,
            )
        return await self._execute_builtin(
            definition,
            arguments,
            context,
            deadline_seconds,
        )

    async def _execute_mcp(
        self,
        definition: RuntimeToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        deadline_seconds: float,
        run_remaining_seconds: float,
    ) -> ToolExecutionResult:
        loop = asyncio.get_running_loop()
        # 同时跟踪业务截止时间与 Run 总截止时间，交付前鉴权也受其约束。
        started_at = loop.time()
        business_deadline = started_at + deadline_seconds
        run_deadline = started_at + max(0.0, run_remaining_seconds)
        # 调用前鉴权：登录会话或聊天权限失效则拒绝执行。
        before = await self.authorize(
            context.owner_id,
            context.chat_session_id,
            context.run_id,
            context.auth_session_id,
        )
        if before is None:
            return ToolExecutionResult(
                _error("tool_permission_denied", "当前登录或聊天权限已失效。"),
                "run_auth_session_invalid",
            )
        source = cast(McpToolSource, definition.source)
        # 调用前实时复核 MCP Server 组织可见性，员工调岗或撤权时立即阻断外部调用。
        if self._access_policy is not None:
            if not await self._access_policy.can_read_resource(
                context.owner_id,
                ResourceType.MCP_SERVER,
                source.server_id,
            ):
                return ToolExecutionResult(
                    _error("tool_permission_denied", "当前用户已失去该 MCP 工具的访问权限。")
                )
        # 按冻结的 server/remote/config 版本重新解析调用目标，配置变更即视为不可用。
        target = await self._mcp_catalog.get_call_target(
            definition.tool_id,
            source.server_id,
            source.remote_name,
            source.config_version,
        )
        if target is None:
            return ToolExecutionResult(_error("tool_unavailable", "工具配置已变更或工具已不可用。"))
        remaining = business_deadline - loop.time()
        if remaining <= 0:
            return ToolExecutionResult(
                _error("tool_deadline_exhausted", "工具执行前 Run 的执行时间已经耗尽。")
            )
        # 只发起一次外部调用，不重试；超时与错误由 MCP envelope 收敛。
        result = await self._mcp_executor.call(
            target,
            dict(arguments),
            deadline_seconds=remaining,
        )
        outcome = _mcp_outcome(result)
        revoked_result = ToolExecutionResult(
            _error("tool_permission_revoked", "Tool permission revoked before delivery."),
            MCP_DELIVERY_AUTH_ERROR,
        )
        # 结果返回后再鉴权：登录态失效或 MCP 部门授权在执行期间被撤回时一律丢弃结果。
        remaining_after_result = run_deadline - loop.time()
        if remaining_after_result <= 0:
            return revoked_result
        try:
            async with asyncio.timeout(
                min(remaining_after_result, MCP_DELIVERY_AUTH_TIMEOUT_SECONDS)
            ):
                authorized = await self.authorize(
                    context.owner_id,
                    context.chat_session_id,
                    context.run_id,
                    context.auth_session_id,
                )
                mcp_allowed = (
                    True
                    if self._access_policy is None
                    else await self._access_policy.can_read_resource(
                        context.owner_id,
                        ResourceType.MCP_SERVER,
                        source.server_id,
                    )
                )
        except TimeoutError:
            return revoked_result
        except Exception as error:
            logger.warning(
                "mcp_delivery_auth_failed exception_type=%s",
                type(error).__name__,
            )
            return revoked_result
        if authorized is None or not mcp_allowed:
            return revoked_result
        return ToolExecutionResult(outcome)

    async def _execute_builtin(
        self,
        definition: RuntimeToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        deadline_seconds: float,
    ) -> ToolExecutionResult:
        source = cast(BuiltinToolSource, definition.source)
        # 每个内置工具独立校验版本；旧 Run 的冻结快照不得静默进入新实现。
        if source.version != _current_builtin_version(definition):
            return ToolExecutionResult(
                _error(
                    "tool_version_unavailable",
                    "内置工具版本已更新，当前冻结调用不会执行。",
                )
            )
        # 执行前鉴权。
        before = await self.authorize(
            context.owner_id,
            context.chat_session_id,
            context.run_id,
            context.auth_session_id,
        )
        if before is None:
            return ToolExecutionResult(
                _error("tool_permission_denied", "当前登录或聊天权限已失效。"),
                "run_auth_session_invalid",
            )
        # 澄清与子智能体调度工具由执行器单独处理，运行时不允许直接调用。
        if definition.model_name == "request_user_input":
            return ToolExecutionResult(
                _error("tool_unavailable", "澄清工具只能由 Run 执行器处理。")
            )
        if definition.model_name == "dispatch_subagents":
            return ToolExecutionResult(
                _error("tool_unavailable", "子智能体调度工具只能由 Run 执行器处理。")
            )
        if definition.tool_id == SKILL_EXECUTE_TOOL_ID:
            if self._skill_execution is None:
                return ToolExecutionResult(_error("tool_unavailable", "脚本执行能力未启用。"))
            skill_id = str(arguments.get("skill_id", ""))
            # 执行前实时复核用户是否仍对该 Skill 拥有读权限（防调岗或部门撤权后继续执行）。
            if self._access_policy is not None and not await self._access_policy.can_read_resource(
                context.owner_id,
                ResourceType.SKILL,
                skill_id,
            ):
                return ToolExecutionResult(
                    _error("tool_permission_denied", "当前用户已失去该 Skill 的访问权限。")
                )
            result = await self._skill_execution.execute(arguments, context)
            after = await self.authorize(
                context.owner_id,
                context.chat_session_id,
                context.run_id,
                context.auth_session_id,
            )
            skill_still_allowed = (
                True
                if self._access_policy is None
                else await self._access_policy.can_read_resource(
                    context.owner_id,
                    ResourceType.SKILL,
                    skill_id,
                )
            )
            if after is None or not skill_still_allowed:
                completion = result.skill_execution_completion
                if completion is not None:
                    completion = completion.model_copy(
                        update={
                            "status": SkillExecutionStatus.FAILED,
                            "error_code": "script_delivery_auth_revoked",
                        }
                    )
                if result.artifacts and self._session_files is not None:
                    await self._session_files.abandon_artifacts(result.artifacts)
                return ToolExecutionResult(
                    outcome=_error(
                        "tool_permission_revoked",
                        "交付前权限复核失败，脚本结果与文件已丢弃。",
                    ),
                    skill_execution_completion=completion,
                    terminate_run_error_code="run_auth_session_invalid",
                )
            return result
        # 内置工具按冻结身份分派到对应实现。
        if definition.tool_id == SAVE_USER_MEMORY_TOOL_ID:
            outcome = await self._save_user_memory(before, arguments)
        elif definition.model_name == "knowledge_search":
            outcome = await self._knowledge_search(
                before,
                arguments,
                deadline_seconds,
                knowledge_base_ids=context.knowledge_base_ids,
            )
        elif definition.tool_id == SKILL_LOAD_TOOL_ID:
            outcome = await self._skill_load(context, arguments)
        elif definition.tool_id == SKILL_READ_RESOURCE_TOOL_ID:
            outcome = await self._skill_read_resource(context, arguments)
        elif definition.tool_id == SESSION_FILE_LIST_TOOL_ID:
            outcome = await self._session_file_list(before, arguments)
        elif definition.tool_id == SESSION_FILE_READ_TOOL_ID:
            outcome = await self._session_file_read(before, arguments)
        else:
            return ToolExecutionResult(_error("tool_unavailable", "内置工具处理器不可用。"))
        # 交付前再次鉴权：权限在工具执行期间被撤销时丢弃结果。
        after = await self.authorize(
            context.owner_id,
            context.chat_session_id,
            context.run_id,
            context.auth_session_id,
        )
        if after is None:
            return ToolExecutionResult(
                _error("tool_permission_revoked", "交付前权限复核失败，结果已丢弃。"),
                "run_auth_session_invalid",
            )
        return ToolExecutionResult(bounded_outcome(outcome))

    async def _save_user_memory(
        self,
        authorized: AuthorizedRunContext,
        arguments: Mapping[str, Any],
    ) -> ToolOutcome:
        """响应对话中用户显式记住诉求，写入一条 chat_explicit 备忘记忆。"""
        if self._preferences is None:
            return _error("user_memory_unavailable", "显式记忆能力未启用。")
        try:
            memory = await self._preferences.create_memory(
                authorized.owner_id,
                str(arguments["content"]),
                source=UserMemorySource.CHAT_EXPLICIT,
            )
        except UserMemoryDisabledError:
            return _error(
                "user_memory_disabled",
                "用户已关闭显式记忆功能，未保存该条记忆。",
            )
        except UserMemoryLimitReachedError:
            return _error(
                "user_memory_limit_reached",
                f"用户的显式记忆数量已达上限（{self._preferences.max_memories} 条），"
                "请提示用户在个人设置中清理后再保存。",
            )
        except InvalidUserMemoryContentError:
            return _error(
                "user_memory_content_invalid",
                "记忆内容不合法，需为 1 到 500 个字符且不能包含非法控制字符。",
            )
        except Exception:
            return _error(
                "user_memory_unavailable",
                "显式记忆暂时不可用，未保存该条记忆。",
            )
        return ToolOutcome(
            status=ToolOutcomeStatus.OK,
            code="user_memory_saved",
            message="已保存用户的显式备忘记忆。",
            data={
                "id": memory.id,
                "content": memory.content,
                "source": memory.source.value,
                "created_at": memory.created_at.isoformat(),
                "updated_at": memory.updated_at.isoformat(),
            },
        )

    async def _knowledge_search(
        self,
        context: AuthorizedRunContext,
        arguments: Mapping[str, Any],
        deadline_seconds: float,
        *,
        knowledge_base_ids: tuple[str, ...] | None = None,
    ) -> ToolOutcome:
        del deadline_seconds
        if self._knowledge is None:
            return _error("knowledge_unavailable", "当前部署未启用知识库检索能力。")
        # 执行前实时解析并鉴权有效知识库集合（支持无项目共享库、项目并集与显式限定）。
        try:
            before_scope = await self._resolve_knowledge_scope(
                context.owner_id,
                context.project_id,
                knowledge_base_ids,
                strict_project_limit=True,
                ignore_missing_project=False,
            )
        except Exception:
            return _error("knowledge_unavailable", "知识库权限范围暂时不可用。")
        if before_scope.error_code is not None or not before_scope.effective_ids:
            return _error(
                before_scope.error_code or "knowledge_unavailable",
                before_scope.error_message or "当前对话没有可查询的知识库。",
            )
        base_ids = list(before_scope.effective_ids)
        query = str(arguments["query"]).strip()
        try:
            limit = _bounded_limit(arguments.get("limit"), default=5)
        except ValueError:
            return _error("tool_arguments_invalid", "limit 必须是 1 到 8 之间的整数。")
        try:
            result = await self._knowledge.search(
                context.owner_id,
                base_ids,
                query,
                limit=limit,
                context_budget=2000,
            )
        except (KnowledgeInputError, KnowledgeNotFoundError):
            return _error("knowledge_scope_unavailable", "知识库查询范围已不可用。")
        except KnowledgeSearchUnavailableError:
            return _error("knowledge_unavailable", "知识检索暂时不可用。")
        except Exception:
            return _error("knowledge_unavailable", "知识检索暂时不可用。")
        # 交付前再次实时复核知识库权限范围（含无项目共享库与显式模式），
        # 防止检索期间员工调岗、部门撤权或解绑导致结果越权交付。
        try:
            after_scope = await self._resolve_knowledge_scope(
                context.owner_id,
                context.project_id,
                knowledge_base_ids,
                strict_project_limit=True,
                ignore_missing_project=False,
            )
        except Exception:
            return _error(
                "knowledge_scope_unavailable",
                "知识库权限复核失败，检索结果已丢弃。",
            )
        searched_ids = set(base_ids)
        if not searched_ids <= after_scope.allowed_ids or any(
            hit.knowledge_base_id not in searched_ids for hit in result.hits
        ):
            return _error(
                "knowledge_scope_unavailable",
                "知识库访问范围已变化，检索结果已丢弃。",
            )
        return _knowledge_outcome(result)

    async def _skill_load(
        self,
        context: ToolExecutionContext,
        arguments: Mapping[str, Any],
    ) -> ToolOutcome:
        if self._skills is None or context.skill_snapshot is None:
            return _error("skill_snapshot_unavailable", "当前 Run 没有固定 Skill 目录。")
        skill_id = str(arguments["skill_id"])
        # 执行期复核部门/角色权限，确保员工调岗后无法加载旧快照中的原部门 Skill。
        if self._access_policy is not None and not await self._access_policy.can_read_resource(
            context.owner_id,
            ResourceType.SKILL,
            skill_id,
        ):
            return _error("tool_permission_denied", "当前用户已失去该 Skill 的访问权限。")
        try:
            entry, version = await self._skills.resolve_run_entry(
                context.skill_snapshot,
                skill_id,
            )
        except SkillNotFoundError:
            return _error("skill_not_in_run_snapshot", "Skill 不在当前 Run 固定目录中。")
        return ToolOutcome(
            status=ToolOutcomeStatus.OK,
            code="skill_loaded",
            message=f"已加载 Skill：{entry.name}。",
            data={
                "skill_id": entry.skill_id,
                "name": entry.name,
                "description": entry.description,
                "version_id": version.id,
                "version_label": version.label,
                "resources": [
                    {
                        "path": item.path,
                        "size_bytes": item.size_bytes,
                        "text": item.text,
                    }
                    for item in version.manifest.files
                    if item.path != "SKILL.md"
                ],
                "scripts": [
                    {
                        "path": item.path,
                        "label": item.label,
                        "purpose": item.purpose,
                        "parameters": item.parameters,
                    }
                    for item in version.manifest.scripts
                ],
            },
        )

    async def _skill_read_resource(
        self,
        context: ToolExecutionContext,
        arguments: Mapping[str, Any],
    ) -> ToolOutcome:
        if self._skills is None or context.skill_snapshot is None:
            return _error("skill_snapshot_unavailable", "当前 Run 没有固定 Skill 目录。")
        skill_id = str(arguments["skill_id"])
        # 执行期复核权限，员工调岗后不得再读取原部门 Skill 包内资源文件。
        if self._access_policy is not None and not await self._access_policy.can_read_resource(
            context.owner_id,
            ResourceType.SKILL,
            skill_id,
        ):
            return _error("tool_permission_denied", "当前用户已失去该 Skill 的访问权限。")
        try:
            resource = await self._skills.read_resource(
                context.skill_snapshot,
                skill_id=skill_id,
                path=str(arguments["path"]),
                offset=int(arguments.get("offset", 0)),
                limit_bytes=int(arguments.get("limit_bytes", 65536)),
            )
        except (SkillNotFoundError, ValueError) as error:
            return _skill_resource_error(error)
        return ToolOutcome(
            status=ToolOutcomeStatus.OK,
            code="skill_resource_read",
            message="已读取 Skill 文本资源。",
            data={
                "skill_id": resource.skill_id,
                "skill_version_id": resource.skill_version_id,
                "path": resource.path,
                "offset": resource.offset,
                "next_offset": resource.next_offset,
                "eof": resource.eof,
                "truncated": resource.next_offset < resource.size_bytes,
                "size_bytes": resource.size_bytes,
                "content": resource.content,
            },
        )

    async def _session_file_list(
        self,
        context: AuthorizedRunContext,
        arguments: Mapping[str, Any],
    ) -> ToolOutcome:
        if self._session_files is None:
            return _error("file_tool_unavailable", "会话文件能力未启用。")
        try:
            limit = _bounded_integer(arguments.get("limit"), default=50, minimum=1, maximum=100)
            page = await self._session_files.list(
                context.chat_session_id,
                context.owner_id,
                after_file_id=cast(str | None, arguments.get("after_file_id")),
                limit=limit,
            )
        except ValueError:
            return _error("session_file_cursor_invalid", "文件分页参数不合法。")
        except Exception:
            return _error("session_file_unavailable", "会话文件暂时不可用。")
        return ToolOutcome(
            status=ToolOutcomeStatus.OK,
            code="session_files_listed",
            message="已列出当前会话文件。",
            data={
                "items": [_session_file_data(item) for item in page.items],
                "next_after_file_id": page.next_after_file_id,
            },
        )

    async def _session_file_read(
        self,
        context: AuthorizedRunContext,
        arguments: Mapping[str, Any],
    ) -> ToolOutcome:
        if self._session_files is None:
            return _error("file_tool_unavailable", "会话文件能力未启用。")
        try:
            result = await self._session_files.read_text(
                context.chat_session_id,
                context.owner_id,
                str(arguments["file_id"]),
                offset=int(arguments.get("offset", 0)),
                limit_bytes=int(arguments.get("limit_bytes", 65536)),
            )
        except TextFileReadError as error:
            return ToolOutcome(
                status=ToolOutcomeStatus.ERROR,
                code=error.code,
                message=(
                    "该文件不是可读取的 UTF-8 文本文件，请使用 Skill 脚本处理。"
                    if error.code == "file_not_text"
                    else "文件文本读取范围或内容不合法。"
                ),
                data={"file": None if error.file is None else _session_file_data(error.file)},
            )
        except (FileInputError, ValueError):
            return _error("session_file_read_failed", "会话文件读取失败。")
        return ToolOutcome(
            status=ToolOutcomeStatus.OK,
            code="session_file_read",
            message="已读取当前会话文件。",
            data={
                "file": _session_file_data(result.file),
                "offset": result.offset,
                "next_offset": result.next_offset,
                "content": result.content,
                "eof": result.eof,
                "truncated": result.truncated,
            },
        )


def _knowledge_outcome(result: SearchResult) -> ToolOutcome:
    items = [
        {
            "base_id": hit.knowledge_base_id,
            "document_id": hit.document_id,
            "version_id": hit.version_id,
            "chunk_ids": list(hit.chunk_ids or (hit.chunk_id,)),
            "title": hit.title,
            "text": hit.text,
            "locators": [
                {
                    "kind": locator.kind,
                    "page": locator.page,
                    "start_line": locator.start_line,
                    "end_line": locator.end_line,
                    "heading": locator.heading,
                    "sheet": locator.sheet,
                    "cell_range": locator.cell_range,
                    "slide": locator.slide,
                    "shape": locator.shape,
                    "media_id": locator.media_id,
                }
                for locator in hit.locators
            ],
            "channels": list(hit.channels),
        }
        for hit in result.hits
    ]
    status = "degraded" if result.degraded_reasons else "ok"
    return ToolOutcome(
        status=ToolOutcomeStatus.OK,
        code="knowledge_degraded" if status == "degraded" else "knowledge_search_ok",
        message=(
            "检索结果可能不完整。"
            if status == "degraded"
            else ("未找到相关知识片段。" if not items else "已找到相关知识片段。")
        ),
        data={
            "status": status,
            "items": items,
            "degraded_reasons": list(result.degraded_reasons),
            "timed_out": False,
        },
    )


def _mcp_outcome(result: McpCallResult) -> ToolOutcome:
    status = {
        McpCallStatus.OK: ToolOutcomeStatus.OK,
        McpCallStatus.ERROR: ToolOutcomeStatus.ERROR,
        McpCallStatus.UNKNOWN: ToolOutcomeStatus.UNKNOWN,
        McpCallStatus.CANCELLED: ToolOutcomeStatus.CANCELLED,
    }[result.status]
    data: dict[str, Any] = {}
    if result.text:
        data["text"] = result.text
    if result.structured is not None:
        data["structured"] = result.structured
    if result.unsupported_content:
        data["unsupported_content"] = list(result.unsupported_content)
    if result.truncated:
        data["truncated"] = True
    return bounded_outcome(
        ToolOutcome(
            status=status,
            code=result.code,
            message=result.message[:2000],
            data=data,
            truncated=result.truncated,
            cleanup_error_code=result.cleanup_error_code,
        )
    )


def _error(code: str, message: str) -> ToolOutcome:
    return ToolOutcome(
        status=ToolOutcomeStatus.ERROR,
        code=code,
        message=message[:2000],
    )


def _current_builtin_version(definition: RuntimeToolDefinition) -> int:
    """返回当前实现要求的内置工具版本。"""
    del definition
    return BUILTIN_VERSION


def _skill_preparation_error(error: Exception) -> ToolOutcome:
    """把 Skill 预校验异常映射为不带路径或包正文的稳定错误。"""
    code = public_skill_error_code(error, fallback="skill_preparation_failed")
    messages = {
        "skill_not_in_run_snapshot": "Skill 不在当前 Run 固定目录中。",
        "skill_version_unavailable": "当前 Run 固定的 Skill 版本不可用。",
        "skill_script_not_registered": "请求的脚本未在该 Skill 中登记。",
        "skill_parameters_invalid": "脚本参数不符合登记结构。",
        "skill_dependency_unsatisfied": "当前执行环境不满足 Skill 依赖。",
        "session_file_not_found": "输入文件不存在或已失去当前会话权限。",
        "session_file_ids_duplicate": "输入文件 ID 不能重复。",
    }
    return _error(code, messages.get(code, "Skill 调用准备失败。"))


def _skill_resource_error(error: Exception) -> ToolOutcome:
    """只把已登记资源错误投影给模型，未知异常正文不会进入结果。"""
    code = public_skill_error_code(error, fallback="skill_resource_unavailable")
    messages = {
        "skill_resource_not_found": "Skill 资源不存在。",
        "skill_resource_not_text": "请求的资源不是可分段读取的文本。",
        "skill_resource_offset_invalid": "资源读取位置无效。",
        "skill_resource_range_invalid": "资源读取范围无效。",
        "skill_resource_too_large": "Skill 资源超过单次读取上限。",
    }
    return _error(code, messages.get(code, "Skill 资源暂时不能读取。"))


def _session_file_data(file: SessionFile) -> dict[str, object]:
    """只投影可下载文件的安全元数据，不暴露对象键或宿主路径。"""
    return {
        "file_id": file.id,
        "filename": file.filename,
        "label": file.label,
        "mime_type": file.mime_type,
        "size_bytes": file.size_bytes,
        "source": file.source.value,
        "created_at": file.created_at.isoformat(),
    }


def _bounded_limit(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8:
        raise ValueError("tool_arguments_invalid")
    return value


def _bounded_integer(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """校验 JSON 整数边界，拒绝 bool 冒充整数。"""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("tool_arguments_invalid")
    return value


@lru_cache(maxsize=512)
def _compiled_validator(schema_json: str) -> Draft202012Validator:
    """缓存已校验的 JSON Schema 编译结果，避免每次工具调用重复执行 check_schema。"""
    schema = json.loads(schema_json)
    if _contains_external_ref(schema):
        raise ValueError("tool_schema_external_ref")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> str | None:
    """Validate an object locally and reject any external schema reference."""
    if not isinstance(arguments, dict):
        return "工具参数必须是 JSON 对象。"
    try:
        schema_json = json.dumps(dict(schema), ensure_ascii=False, sort_keys=True)
        validator = _compiled_validator(schema_json)
        error = next(iter(validator.iter_errors(dict(arguments))), None)
    except ValueError as error:
        if str(error) == "tool_schema_external_ref":
            return "工具参数的 Schema 包含不允许的外部引用。"
        return "工具参数的 Schema 无法安全校验。"
    except Exception:
        return "工具参数的 Schema 无法安全校验。"
    if error is None:
        return None
    path = ".".join(str(item) for item in error.absolute_path)
    return f"工具参数不符合 Schema：{path or 'root'}"


def _contains_external_ref(value: object) -> bool:
    if isinstance(value, Mapping):
        ref = value.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            return True
        dynamic_ref = value.get("$dynamicRef")
        if isinstance(dynamic_ref, str) and not dynamic_ref.startswith("#"):
            return True
        return any(_contains_external_ref(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_external_ref(item) for item in value)
    return False


__all__ = [
    "KNOWLEDGE_SEARCH_DEFINITION",
    "KNOWLEDGE_SEARCH_ID",
    "MCP_DELIVERY_AUTH_TIMEOUT_SECONDS",
    "MCP_TOOL_WATCHDOG_GRACE_SECONDS",
    "REQUEST_USER_INPUT_DEFINITION",
    "REQUEST_USER_INPUT_ID",
    "SAVE_USER_MEMORY_DEFINITION",
    "SAVE_USER_MEMORY_TOOL_ID",
    "SESSION_FILE_LIST_DEFINITION",
    "SESSION_FILE_LIST_TOOL_ID",
    "SESSION_FILE_READ_DEFINITION",
    "SESSION_FILE_READ_TOOL_ID",
    "SKILL_EXECUTE_DEFINITION",
    "SKILL_EXECUTE_TOOL_ID",
    "SKILL_LOAD_DEFINITION",
    "SKILL_LOAD_TOOL_ID",
    "SKILL_READ_RESOURCE_DEFINITION",
    "SKILL_READ_RESOURCE_TOOL_ID",
    "RunToolRuntimeService",
]
