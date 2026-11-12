"""Unified Run tool catalog, authorization, and single-call dispatch."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC
from typing import Any, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from app.file.service import FileInputError, SessionFileService
from app.knowledge.service import (
    KnowledgeInputError,
    KnowledgeNotFoundError,
    KnowledgeSearchUnavailableError,
    KnowledgeService,
)
from app.memory.service import MemoryService
from app.project.knowledge import KnowledgeNotFoundError as ProjectKnowledgeNotFoundError
from app.project.knowledge import ProjectKnowledgeService
from app.skill.errors import public_skill_error_code
from app.skill.execution_service import SkillExecutionService
from app.skill.service import SkillService
from app.skill.tooling import summarize_parameters
from app.tool.ports import RunToolRuntime
from common.date import utc_now
from common.json import utf8_size
from domain.auth.ports import CurrentSessionReader
from domain.file import SessionFile, TextFileReadError
from domain.knowledge.model import SearchResult
from domain.mcp import (
    McpCallResult,
    McpCallStatus,
    McpToolExecutor,
    McpToolRuntimeReader,
)
from domain.memory.model import AccessContext, RecallRequest
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
    SafeSkillApprovalProjection,
    SafeSkillInputFile,
    SkillDependencyUnsatisfiedError,
    SkillExecutionStatus,
    SkillNotFoundError,
    SkillPackageError,
)
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
MEMORY_SEARCH_ID = "builtin_memory_search"
KNOWLEDGE_SEARCH_ID = "builtin_knowledge_search"
BUILTIN_VERSION = 1
MCP_DELIVERY_AUTH_TIMEOUT_SECONDS = 1.0
MCP_TOOL_WATCHDOG_GRACE_SECONDS = 0.25
MCP_DELIVERY_AUTH_ERROR = "run_auth_session_invalid"

logger = logging.getLogger(__name__)

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

MEMORY_SEARCH_DEFINITION = RuntimeToolDefinition(
    tool_id=MEMORY_SEARCH_ID,
    model_name="memory_search",
    description=(
        "检索当前用户及当前项目范围内已确认的长期记忆，用于回答用户偏好、历史约定"
        "或项目决定。结果是历史参考，不能覆盖当前用户指示；可能没有命中。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 2000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    source=BuiltinToolSource(version=BUILTIN_VERSION),
    requires_approval=False,
)

KNOWLEDGE_SEARCH_DEFINITION = RuntimeToolDefinition(
    tool_id=KNOWLEDGE_SEARCH_ID,
    model_name="knowledge_search",
    description=(
        "检索当前聊天项目已关联且当前用户仍有权访问的知识库。只返回安全的文档标题、"
        "片段和来源标识；零命中与检索不可用必须区分。"
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


class RunToolRuntimeService(RunToolRuntime):
    """Build a frozen catalog and execute one validated tool without retries."""

    def __init__(
        self,
        *,
        auth: CurrentSessionReader,
        sessions: SessionRepository,
        mcp_catalog: McpToolRuntimeReader,
        mcp_executor: McpToolExecutor,
        memory: MemoryService | None,
        memory_enabled: bool,
        knowledge: KnowledgeService | None,
        project_knowledge: ProjectKnowledgeService | None,
        skills: SkillService | None = None,
        session_files: SessionFileService | None = None,
        skill_execution: SkillExecutionService | None = None,
        clock: Any = utc_now,
    ) -> None:
        self._auth = auth
        self._sessions = sessions
        self._mcp_catalog = mcp_catalog
        self._mcp_executor = mcp_executor
        self._memory = memory
        self._memory_enabled = memory_enabled
        self._knowledge = knowledge
        self._project_knowledge = project_knowledge
        self._skills = skills
        self._session_files = session_files
        self._skill_execution = skill_execution
        self._clock = clock

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

    async def build_snapshot(
        self,
        prompt_template_version: str,
        context: AuthorizedRunContext,
    ) -> RuntimeToolSnapshot:
        """Freeze the complete v2 tool catalog."""
        # 只支持当前模板版本；其余版本（含历史 v1）统一拒绝。
        if prompt_template_version != "run-v2":
            raise ValueError("run_prompt_template_unsupported")

        definitions: list[RuntimeToolDefinition] = [REQUEST_USER_INPUT_DEFINITION]
        # 按部署开关与项目绑定关系逐步加入内置工具。
        if self._memory_enabled and self._memory is not None:
            definitions.append(MEMORY_SEARCH_DEFINITION)
        # 只有绑定了项目且存在知识库时才暴露 knowledge_search。
        if (
            context.project_id
            and self._knowledge is not None
            and self._project_knowledge is not None
        ):
            try:
                bases = await self._project_knowledge.list(context.owner_id, context.project_id)
            except (ProjectNotFoundError, ProjectKnowledgeNotFoundError):
                bases = []
            if bases:
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
        # 合并 MCP 目录中的工具定义。
        mcp_tools = await self._mcp_catalog.list_runtime_tools(limit=201)
        for item in mcp_tools:
            definitions.append(
                RuntimeToolDefinition(
                    tool_id=item.tool_id,
                    model_name=item.model_name,
                    description=item.description,
                    input_schema=dict(item.input_schema),
                    source=McpToolSource(
                        server_id=item.server_id,
                        remote_name=item.remote_name,
                        config_version=item.config_version,
                    ),
                    requires_approval=item.requires_approval,
                )
            )
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
        try:
            entry, version, script, _dependency_check = (
                await self._skills.prepare_script_target(
                    snapshot,
                    skill_id=skill_id,
                    script_path=script_path,
                    parameters=cast(dict[str, Any], parameters),
                )
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
        """从 Run 固定快照和持久化成功加载证据重建指南。"""
        # 空固定目录表示 Run 未启用 Skill，不要求装配 SkillService。
        if run.skill_snapshot is None or not run.skill_snapshot.skills:
            return RunSkillPromptContext()
        if self._skills is None:
            raise ValueError("skill_runtime_unavailable")
        selected = set(run.skill_snapshot.selected_skill_ids)
        active_ids = tuple(
            item.skill_id
            for item in run.skill_snapshot.skills
            if item.skill_id in selected or item.skill_id in set(loaded_skill_ids)
        )
        return await self._skills.prompt_context(run.skill_snapshot, active_ids)

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
        # 按冻结的 JSON Schema 校验参数形状。
        validation_error = _validate_arguments(definition.input_schema, arguments)
        if validation_error is not None:
            return _error("tool_arguments_invalid", validation_error)
        query = arguments.get("query")
        if (
            definition.model_name in {"memory_search", "knowledge_search"}
            and isinstance(query, str)
            and not query.strip()
        ):
            return _error("tool_arguments_invalid", "查询内容不能为空。")
        return None

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
        # 结果返回后再鉴权：交付前权限失效则丢弃结果并终止 Run。
        remaining_after_result = run_deadline - loop.time()
        if remaining_after_result <= 0:
            return ToolExecutionResult(outcome, MCP_DELIVERY_AUTH_ERROR)
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
        except TimeoutError:
            return ToolExecutionResult(outcome, MCP_DELIVERY_AUTH_ERROR)
        except Exception as error:
            logger.warning(
                "mcp_delivery_auth_failed exception_type=%s",
                type(error).__name__,
            )
            return ToolExecutionResult(outcome, MCP_DELIVERY_AUTH_ERROR)
        if authorized is None:
            return ToolExecutionResult(outcome, MCP_DELIVERY_AUTH_ERROR)
        return ToolExecutionResult(outcome)

    async def _execute_builtin(
        self,
        definition: RuntimeToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        deadline_seconds: float,
    ) -> ToolExecutionResult:
        source = cast(BuiltinToolSource, definition.source)
        # 冻结目录中的内置工具版本必须与当前实现一致。
        if source.version != BUILTIN_VERSION:
            return ToolExecutionResult(_error("tool_unavailable", "内置工具版本不受支持。"))
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
        # 澄清工具由执行器单独处理，运行时不允许直接调用。
        if definition.model_name == "request_user_input":
            return ToolExecutionResult(
                _error("tool_unavailable", "澄清工具只能由 Run 执行器处理。")
            )
        if definition.tool_id == SKILL_EXECUTE_TOOL_ID:
            if self._skill_execution is None:
                return ToolExecutionResult(
                    _error("tool_unavailable", "脚本执行能力未启用。")
                )
            result = await self._skill_execution.execute(arguments, context)
            after = await self.authorize(
                context.owner_id,
                context.chat_session_id,
                context.run_id,
                context.auth_session_id,
            )
            if after is None:
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
        # 内置工具按冻结名称分派到对应实现。
        if definition.model_name == "memory_search":
            outcome = await self._memory_search(before, arguments)
        elif definition.model_name == "knowledge_search":
            outcome = await self._knowledge_search(before, arguments, deadline_seconds)
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

    async def _memory_search(
        self,
        context: AuthorizedRunContext,
        arguments: Mapping[str, Any],
    ) -> ToolOutcome:
        if not self._memory_enabled or self._memory is None:
            return _error("memory_disabled", "记忆检索未启用。")
        query = str(arguments["query"]).strip()
        try:
            limit = _bounded_limit(arguments.get("limit"), default=5)
        except ValueError:
            return _error("tool_arguments_invalid", "limit 必须是 1 到 8 之间的整数。")
        scopes = ["personal"]
        priority = ["personal"]
        if context.project_id:
            project_scope = f"project:{context.project_id}"
            scopes.append(project_scope)
            priority = [project_scope, "personal"]
        try:
            result = await self._memory.recall(
                RecallRequest(
                    access=AccessContext(
                        namespace_id=context.owner_id,
                        readable_scopes=tuple(scopes),
                        writable_scopes=(),
                    ),
                    scope_keys=tuple(scopes),
                    query=query,
                    scope_priority=tuple(priority),
                    limit=limit,
                    token_budget=1200,
                )
            )
        except Exception:
            return _error("memory_unavailable", "记忆检索暂时不可用。")
        if result.status == "disabled":
            return _error("memory_disabled", "用户已关闭记忆读取。")
        if result.status == "unavailable":
            return _error("memory_unavailable", "记忆检索暂时不可用。")
        items = [
            {
                "memory_id": hit.memory.id,
                "version": hit.memory.version,
                "content": hit.memory.content,
                "scope": "project" if hit.memory.scope_key.startswith("project:") else "personal",
            }
            for hit in result.hits
        ]
        status = ToolOutcomeStatus.OK
        message = (
            "检索结果可能不完整。"
            if result.status == "degraded"
            else ("未找到相关的已确认记忆。" if not items else "已找到相关记忆。")
        )
        return ToolOutcome(
            status=status,
            code="memory_degraded" if result.status == "degraded" else "memory_search_ok",
            message=message,
            data={
                "status": result.status,
                "items": items,
                "truncated": result.truncated,
                "degradation_reasons": list(result.degradation_reasons),
            },
            truncated=result.truncated,
        )

    async def _knowledge_search(
        self,
        context: AuthorizedRunContext,
        arguments: Mapping[str, Any],
        deadline_seconds: float,
    ) -> ToolOutcome:
        if self._knowledge is None or self._project_knowledge is None or context.project_id is None:
            return _error("knowledge_unavailable", "当前聊天没有可查询的项目知识库。")
        try:
            bases = await self._project_knowledge.list(context.owner_id, context.project_id)
        except Exception:
            return _error("knowledge_unavailable", "项目知识库权限暂时不可用。")
        if len(bases) > 20:
            return _error(
                "knowledge_scope_unavailable",
                "项目关联的知识库超过单次检索上限。",
            )
        base_ids = [base.id for base in bases]
        if not base_ids:
            return _error("knowledge_unavailable", "当前项目没有关联知识库。")
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
        try:
            current_bases = await self._project_knowledge.list(
                context.owner_id,
                context.project_id,
            )
        except Exception:
            return _error(
                "knowledge_scope_unavailable",
                "项目知识库权限复核失败，检索结果已丢弃。",
            )
        searched_ids = set(base_ids)
        current_ids = {base.id for base in current_bases}
        if not searched_ids <= current_ids or any(
            hit.knowledge_base_id not in searched_ids for hit in result.hits
        ):
            return _error(
                "knowledge_scope_unavailable",
                "项目知识库范围已变化，检索结果已丢弃。",
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
        try:
            resource = await self._skills.read_resource(
                context.skill_snapshot,
                skill_id=str(arguments["skill_id"]),
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
                data={
                    "file": None
                    if error.file is None
                    else _session_file_data(error.file)
                },
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
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError("tool_arguments_invalid")
    return value


def _validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> str | None:
    """Validate an object locally and reject any external schema reference."""
    if not isinstance(arguments, dict):
        return "工具参数必须是 JSON 对象。"
    try:
        if _contains_external_ref(schema):
            return "工具参数的 Schema 包含不允许的外部引用。"
        Draft202012Validator.check_schema(dict(schema))
        validator = Draft202012Validator(dict(schema))
        error = next(iter(validator.iter_errors(dict(arguments))), None)
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
    "MEMORY_SEARCH_DEFINITION",
    "MEMORY_SEARCH_ID",
    "REQUEST_USER_INPUT_DEFINITION",
    "REQUEST_USER_INPUT_ID",
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
