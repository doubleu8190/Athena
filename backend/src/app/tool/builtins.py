"""内置工具定义目录、执行分发器及结果格式化辅助函数。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, cast

from app.access import AccessPolicyService
from app.file.service import FileInputError, SessionFileService
from app.knowledge.service import (
    KnowledgeInputError,
    KnowledgeNotFoundError,
    KnowledgeService,
)
from app.preference import PreferenceService
from app.skill.errors import public_skill_error_code
from app.skill.execution_service import SkillExecutionService
from app.skill.service import SkillService
from app.tool.run_context import KnowledgeScopeResolution
from domain.access import ResourceType
from domain.file import FileStorageError, SessionFile, TextFileReadError
from domain.knowledge.model import SearchResult
from domain.preference import (
    MAX_USER_MEMORY_CONTENT_LENGTH,
    MIN_USER_MEMORY_CONTENT_LENGTH,
    SAVE_USER_MEMORY_TOOL_ID,
    InvalidUserMemoryContentError,
    UserMemoryDisabledError,
    UserMemoryLimitReachedError,
    UserMemorySource,
)
from domain.skill import (
    SESSION_FILE_LIST_TOOL_ID,
    SESSION_FILE_READ_TOOL_ID,
    SKILL_EXECUTE_TOOL_ID,
    SKILL_LOAD_TOOL_ID,
    SKILL_READ_RESOURCE_TOOL_ID,
    SkillNotFoundError,
)
from domain.subagent import DISPATCH_SUBAGENTS_TOOL_ID, SubagentProfile
from domain.tool import (
    AuthorizedRunContext,
    BuiltinToolSource,
    RuntimeToolDefinition,
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
MCP_TOOL_WATCHDOG_GRACE_SECONDS = 0.25

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
    description=(
        "严格按 UTF-8 字符边界分段读取当前会话文件正文："
        "既可读取白名单纯文本文件（TXT/CSV/JSON/MD/LOG），"
        "也可分段读取 PDF、Word（DOCX）、Excel（XLSX）、PPT（PPTX）"
        "自动提取的结构化文本。"
    ),
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


def _error(code: str, message: str) -> ToolOutcome:
    """构造截断长度后的确定性工具错误输出。"""
    return ToolOutcome(
        status=ToolOutcomeStatus.ERROR,
        code=code,
        message=message[:2000],
    )


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


_SESSION_FILE_READ_ERROR_MESSAGES: dict[str, str] = {
    "file_not_text": (
        "该文件不是可直接读取的纯文本或办公文档（如图片文件），请使用 Skill 脚本处理。"
    ),
    "file_offset_invalid": (
        "文件读取偏移量无效，必须位于合法的 UTF-8 字符起始边界且不超过文本总字节数。"
    ),
    "file_range_too_small": (
        "单次读取字节窗口过小，不足以完整解码当前多字节字符，请增大 limit_bytes。"
    ),
    "file_content_not_text": "文件内容不是合法的 UTF-8 文本，无法直接读取。",
    "document_no_extractable_text": (
        "该文档未检测到可提取的文本层（例如纯扫描图片 PDF 或空白文档），无法直接读取文本。"
    ),
    "document_encrypted": "该文档受密码加密保护，无法自动提取文本内容。",
    "document_macro_unsupported": "出于安全限制，包含宏的 Office 文档不支持自动文本提取。",
    "document_page_limit_exceeded": "该 PDF 文档页数超过平台单文件解析上限，无法自动提取文本。",
    "document_extract_too_large": (
        "该文档原件超过单文件自动文本提取大小上限（20 MiB），请改用 Skill 脚本处理。"
    ),
    "document_extracted_text_too_large": (
        "该文档提取后的纯文本体积超过上限，请改用 Skill 脚本分段处理。"
    ),
    "document_archive_limit_exceeded": (
        "该 Office 文档压缩结构或展开体积超过安全上限，无法自动提取文本。"
    ),
    "document_extract_timeout": "文档自动文本提取超时，未能生成可读文本缓存。",
    "document_parse_failed": "文档解析失败（文件可能损坏或格式不兼容），无法自动提取文本。",
}


def _session_file_read_error_message(code: str) -> str:
    """将文本读取与文档提取错误码转译为清晰、安全的中文业务提示。"""
    return _SESSION_FILE_READ_ERROR_MESSAGES.get(code, "文件文本读取范围或内容不合法。")


def _session_file_data(file: SessionFile) -> dict[str, object]:
    """只投影可下载文件的安全元数据，不暴露对象键或宿主路径。"""
    return {
        "file_id": file.id,
        "filename": file.filename,
        "label": file.label,
        "mime_type": file.mime_type,
        "size_bytes": file.size_bytes,
        "source": file.source.value,
        "extraction_status": file.extraction_status.value,
        "extracted_text_bytes": file.extracted_text_bytes,
        "extraction_error_code": file.extraction_error_code,
        "created_at": file.created_at.isoformat(),
    }


def _knowledge_outcome(result: SearchResult) -> ToolOutcome:
    """将领域知识检索结果转换为标准 ToolOutcome 载荷。"""
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


_BuiltinHandler = Callable[
    [AuthorizedRunContext, ToolExecutionContext, Mapping[str, Any], float],
    Awaitable[ToolOutcome],
]


class BuiltinToolExecutor:
    """表驱动分派并执行所有内置业务工具。"""

    def __init__(
        self,
        *,
        knowledge: KnowledgeService | None,
        preferences: PreferenceService | None,
        skills: SkillService | None,
        session_files: SessionFileService | None,
        skill_execution: SkillExecutionService | None,
        access_policy: AccessPolicyService,
        resolve_knowledge_scope: Callable[..., Awaitable[KnowledgeScopeResolution]],
    ) -> None:
        """保存内置工具依赖的服务端口与知识库范围解析器。"""
        self._knowledge = knowledge
        self._preferences = preferences
        self._skills = skills
        self._session_files = session_files
        self._skill_execution = skill_execution
        self._access_policy = access_policy
        self._resolve_knowledge_scope = resolve_knowledge_scope
        self._handlers: dict[str, _BuiltinHandler] = {
            SAVE_USER_MEMORY_TOOL_ID: self._handle_save_user_memory,
            KNOWLEDGE_SEARCH_ID: self._handle_knowledge_search,
            SKILL_LOAD_TOOL_ID: self._handle_skill_load,
            SKILL_READ_RESOURCE_TOOL_ID: self._handle_skill_read_resource,
            SESSION_FILE_LIST_TOOL_ID: self._handle_session_file_list,
            SESSION_FILE_READ_TOOL_ID: self._handle_session_file_read,
        }

    async def execute(
        self,
        definition: RuntimeToolDefinition,
        arguments: Mapping[str, Any],
        context: ToolExecutionContext,
        authorized: AuthorizedRunContext,
        deadline_seconds: float,
    ) -> ToolExecutionResult:
        """按冻结版本与工具 ID 分派执行内置工具（假定鉴权已完成）。"""
        source = cast(BuiltinToolSource, definition.source)
        # 每个内置工具独立校验版本；旧 Run 的冻结快照不得静默进入新实现。
        if source.version != BUILTIN_VERSION:
            return ToolExecutionResult(
                _error(
                    "tool_version_unavailable",
                    "内置工具版本已更新，当前冻结调用不会执行。",
                )
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
            if not await self._access_policy.can_read_resource(
                context.owner_id,
                ResourceType.SKILL,
                skill_id,
            ):
                return ToolExecutionResult(
                    _error("tool_permission_denied", "当前用户已失去该 Skill 的访问权限。")
                )
            return await self._skill_execution.execute(arguments, context)

        # 按冻结工具 ID 查表分派；兼容仅有 model_name == "knowledge_search" 的历史或测试定义。
        handler_key = (
            KNOWLEDGE_SEARCH_ID
            if definition.model_name == "knowledge_search"
            else definition.tool_id
        )
        handler = self._handlers.get(handler_key)
        if handler is None:
            return ToolExecutionResult(_error("tool_unavailable", "内置工具处理器不可用。"))
        outcome = await handler(authorized, context, arguments, deadline_seconds)
        return ToolExecutionResult(bounded_outcome(outcome))

    async def _handle_save_user_memory(
        self,
        authorized: AuthorizedRunContext,
        _context: ToolExecutionContext,
        arguments: Mapping[str, Any],
        _deadline_seconds: float,
    ) -> ToolOutcome:
        return await self._save_user_memory(authorized, arguments)

    async def _handle_knowledge_search(
        self,
        authorized: AuthorizedRunContext,
        context: ToolExecutionContext,
        arguments: Mapping[str, Any],
        deadline_seconds: float,
    ) -> ToolOutcome:
        return await self._knowledge_search(
            authorized,
            arguments,
            deadline_seconds,
            knowledge_base_ids=context.knowledge_base_ids,
        )

    async def _handle_skill_load(
        self,
        _authorized: AuthorizedRunContext,
        context: ToolExecutionContext,
        arguments: Mapping[str, Any],
        _deadline_seconds: float,
    ) -> ToolOutcome:
        return await self._skill_load(context, arguments)

    async def _handle_skill_read_resource(
        self,
        _authorized: AuthorizedRunContext,
        context: ToolExecutionContext,
        arguments: Mapping[str, Any],
        _deadline_seconds: float,
    ) -> ToolOutcome:
        return await self._skill_read_resource(context, arguments)

    async def _handle_session_file_list(
        self,
        authorized: AuthorizedRunContext,
        _context: ToolExecutionContext,
        arguments: Mapping[str, Any],
        _deadline_seconds: float,
    ) -> ToolOutcome:
        return await self._session_file_list(authorized, arguments)

    async def _handle_session_file_read(
        self,
        authorized: AuthorizedRunContext,
        _context: ToolExecutionContext,
        arguments: Mapping[str, Any],
        _deadline_seconds: float,
    ) -> ToolOutcome:
        return await self._session_file_read(authorized, arguments)

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
        # 生产环境无条件装配知识库服务；此处保留保护，兼容未注入知识库依赖的测试或最小运行时。
        if self._knowledge is None:
            return _error("knowledge_unavailable", "知识库检索服务当前不可用。")
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
            # 调用前已由 before_scope 完成实时范围鉴权，直接向 KnowledgeService 透传
            # skip_access_check=True 避免同一调用内重复鉴权。
            result = await self._knowledge.search(
                context.owner_id,
                base_ids,
                query,
                limit=limit,
                context_budget=2000,
                skip_access_check=True,
            )
        except (KnowledgeInputError, KnowledgeNotFoundError):
            return _error("knowledge_scope_unavailable", "知识库查询范围已不可用。")
        except Exception:
            # 含 KnowledgeSearchUnavailableError 及底层意外异常，统一降级为确定性工具错误。
            return _error("knowledge_unavailable", "知识检索暂时不可用。")
        # 校验检索返回命中均落于本次已授权的 searched_ids 范围，拦截越界结果。
        searched_ids = set(base_ids)
        if any(hit.knowledge_base_id not in searched_ids for hit in result.hits):
            return _error(
                "knowledge_scope_unavailable",
                "知识库检索命中范围异常，结果已丢弃。",
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
        if not await self._access_policy.can_read_resource(
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
        if not await self._access_policy.can_read_resource(
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
        except (SkillNotFoundError, ValueError, FileStorageError) as error:
            # 对象存储未启用或读取故障时映射为确定性 ERROR，避免异常逃逸被外层误判为 UNKNOWN。
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
                message=_session_file_read_error_message(error.code),
                data={"file": None if error.file is None else _session_file_data(error.file)},
            )
        except (FileInputError, ValueError):
            return _error("session_file_read_failed", "会话文件读取失败。")
        except Exception:
            return _error("session_file_unavailable", "会话文件暂时不可用。")
        return ToolOutcome(
            status=ToolOutcomeStatus.OK,
            code="session_file_read",
            message="已读取当前会话文件。",
            data={
                "file": _session_file_data(result.file),
                "source_kind": result.source_kind,
                "offset": result.offset,
                "next_offset": result.next_offset,
                "content": result.content,
                "eof": result.eof,
                "truncated": result.truncated,
            },
        )
