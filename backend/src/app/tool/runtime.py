"""Unified Run tool catalog, authorization, and single-call dispatch."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from app.access import AccessPolicyService
from app.file.service import FileInputError, SessionFileService
from app.knowledge.service import KnowledgeService
from app.preference import PreferenceService
from app.project.knowledge import ProjectKnowledgeService
from app.skill.execution_service import SkillExecutionService
from app.skill.service import SkillService
from app.skill.tooling import summarize_parameters
from app.tool.builtins import (
    BUILTIN_VERSION,
    KNOWLEDGE_SEARCH_DEFINITION,
    KNOWLEDGE_SEARCH_ID,
    MCP_TOOL_WATCHDOG_GRACE_SECONDS,
    REQUEST_USER_INPUT_DEFINITION,
    REQUEST_USER_INPUT_ID,
    SAVE_USER_MEMORY_DEFINITION,
    SESSION_FILE_LIST_DEFINITION,
    SESSION_FILE_READ_DEFINITION,
    SKILL_EXECUTE_DEFINITION,
    SKILL_LOAD_DEFINITION,
    SKILL_READ_RESOURCE_DEFINITION,
    BuiltinToolExecutor,
    _error,
    _skill_preparation_error,
    build_dispatch_subagents_definition,
)
from app.tool.builtins import _skill_resource_error as _skill_resource_error
from app.tool.ports import RunToolRuntime
from app.tool.run_context import KnowledgeScopeResolution, RunToolContextBuilder
from common.date import as_utc, utc_now
from common.json import utf8_size
from common.json_schema import compile_draft202012_validator, format_validation_path
from domain.access import ResourceType
from domain.auth.ports import CurrentSessionReader
from domain.file import PendingSessionFile
from domain.mcp import (
    McpCallResult,
    McpCallStatus,
    McpToolExecutor,
    McpToolRuntimeReader,
)
from domain.preference import SAVE_USER_MEMORY_TOOL_ID
from domain.run import Run, RunInputFileContext, RunInputImageAttachment
from domain.session import SessionScope
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
    SkillNotFoundError,
    SkillPackageError,
)
from domain.subagent import DISPATCH_SUBAGENTS_TOOL_ID
from domain.tool import (
    MAX_TOOL_ARGUMENT_BYTES,
    AuthorizedRunContext,
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

if TYPE_CHECKING:
    from adapter.session.repository import SqlAlchemySessionRepository
    from adapter.subagent.repository import SqlAlchemySubagentRepository


class RunToolRuntimeService(RunToolRuntime):
    """Build a frozen catalog and execute one validated tool without retries."""

    def __init__(
        self,
        *,
        auth: CurrentSessionReader,
        sessions: SqlAlchemySessionRepository,
        mcp_catalog: McpToolRuntimeReader,
        mcp_executor: McpToolExecutor,
        knowledge: KnowledgeService | None,
        project_knowledge: ProjectKnowledgeService | None,
        preferences: PreferenceService | None = None,
        skills: SkillService | None = None,
        session_files: SessionFileService | None = None,
        skill_execution: SkillExecutionService | None = None,
        subagents: SqlAlchemySubagentRepository | None = None,
        access_policy: AccessPolicyService,
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
        self._context_builder = RunToolContextBuilder(
            project_knowledge=project_knowledge,
            skills=skills,
            session_files=session_files,
            access_policy=access_policy,
        )
        self._builtin_executor = BuiltinToolExecutor(
            knowledge=knowledge,
            preferences=preferences,
            skills=skills,
            session_files=session_files,
            skill_execution=skill_execution,
            access_policy=access_policy,
            resolve_knowledge_scope=self._context_builder.resolve_knowledge_scope,
        )

    async def can_read_subagent(self, user_id: str, subagent_id: str) -> bool:
        """供 RunExecutor 调度子智能体前复核当前用户对目标 Subagent 的实时读权限。"""
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
        now = as_utc(self._clock())
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
        *,
        depth: int = 0,
        allowed_tool_ids: Sequence[str] | None = None,
        knowledge_base_ids: tuple[str, ...] | None = None,
    ) -> RuntimeToolSnapshot:
        """Freeze the complete v2 tool catalog."""
        # 只支持当前模板版本；其余版本（含历史 v1）统一拒绝。
        if prompt_template_version != "run-v2":
            raise ValueError("run_prompt_template_unsupported")

        # 入口仅加载 1 次实时用户权限快照，统一透传给知识库、MCP 与 Subagent 过滤路径，
        # 避免单次快照构造重复查询用户组织树。
        access_snapshot = await self._access_policy.get_user_snapshot(context.owner_id)
        access_snapshot_loaded = True

        definitions: list[RuntimeToolDefinition] = [REQUEST_USER_INPUT_DEFINITION]
        # 仅主 Run 且用户开启显式记忆开关时，暴露免审批的 save_user_memory 工具。
        if depth == 0 and self._preferences is not None:
            if await self._preferences.is_memory_enabled(context.owner_id):
                definitions.append(SAVE_USER_MEMORY_DEFINITION)
        # 统一解析有效知识库范围：无项目对话只要存在可读共享库或显式指定库，
        # 同样自动暴露 knowledge_search。
        if self._knowledge is not None:
            try:
                scope = await self._context_builder.resolve_knowledge_scope(
                    context.owner_id,
                    context.project_id,
                    knowledge_base_ids,
                    strict_project_limit=False,
                    ignore_missing_project=True,
                    snapshot=access_snapshot,
                    snapshot_loaded=access_snapshot_loaded,
                )
            except Exception:
                scope = KnowledgeScopeResolution(())
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
        # 合并 MCP 目录中的工具定义：通过单 Session SQL EXISTS 下推读取可读工具，
        # 避免未授权部门工具占用 limit=201 额度。
        mcp_tools = (
            ()
            if access_snapshot is None or not access_snapshot.is_active
            else await self._mcp_catalog.list_readable_runtime_tools(
                access_snapshot,
                limit=201,
            )
        )
        definitions.extend(mcp_tools)

        if depth == 0 and self._subagents is not None:
            enabled_subagents = (
                ()
                if access_snapshot is None or not access_snapshot.is_active
                else await self._subagents.list_readable_enabled(access_snapshot)
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
            # 审批准备阶段复用进程级 TTL 缓存探针，真正执行时由 SkillExecutionService 实时复核。
            entry, version, script, _dependency_check = await self._skills.prepare_script_target(
                snapshot,
                skill_id=skill_id,
                script_path=script_path,
                parameters=cast(dict[str, Any], parameters),
                use_cached_dependency_check=True,
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
        """委托 RunToolContextBuilder 重建并过滤当前 Run 活跃技能上下文。"""
        return await self._context_builder.build_skill_context(run, loaded_skill_ids)

    async def build_run_input_file_context(
        self,
        run: Run,
    ) -> tuple[RunInputFileContext, ...]:
        """委托 RunToolContextBuilder 解析 Run 累计绑定文件的安全引用元数据。"""
        return await self._context_builder.build_run_input_file_context(run)

    async def build_run_input_vision_context(
        self,
        run: Run,
        input_files: Sequence[RunInputFileContext],
        *,
        supports_vision: bool,
    ) -> tuple[tuple[RunInputFileContext, ...], tuple[RunInputImageAttachment, ...]]:
        """委托 RunToolContextBuilder 构建视觉图片附件并回填安全状态。"""
        return await self._context_builder.build_run_input_vision_context(
            run,
            input_files,
            supports_vision=supports_vision,
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
        if definition.source.kind == "builtin" and definition.source.version != BUILTIN_VERSION:
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
        """Validate locally, authorize before execution, and dispatch once."""
        del run_remaining_seconds
        # 执行前再校验一次，确保入口调用方无法绕过本地约束。
        validation_error = self.validate_call(definition, model_name, arguments)
        if validation_error is not None:
            return ToolExecutionResult(validation_error)
        assert definition is not None
        # 按来源分派：MCP 按业务与清理两段超时收敛，内置工具委托 BuiltinToolExecutor 执行。
        if definition.source.kind == "mcp":
            return await self._execute_mcp(
                definition,
                arguments,
                context,
                deadline_seconds,
            )
        # 内置工具在执行前先完成实时登录与会话鉴权。
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
        return await self._builtin_executor.execute(
            definition,
            arguments,
            context,
            before,
            deadline_seconds,
        )

    async def _execute_mcp(
        self,
        definition: RuntimeToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        deadline_seconds: float,
    ) -> ToolExecutionResult:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        business_deadline = started_at + deadline_seconds
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
        return ToolExecutionResult(_mcp_outcome(result))


def _mcp_outcome(result: McpCallResult) -> ToolOutcome:
    """将 MCP 执行结果映射为统一的 ToolOutcome。"""
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


def _validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> str | None:
    """Validate an object locally and reject any external schema reference."""
    if not isinstance(arguments, dict):
        return "工具参数必须是 JSON 对象。"
    try:
        schema_json = json.dumps(dict(schema), ensure_ascii=False, sort_keys=True)
        validator = compile_draft202012_validator(schema_json)
        error = next(iter(validator.iter_errors(dict(arguments))), None)
    except ValueError as error:
        if str(error) == "tool_schema_external_ref":
            return "工具参数的 Schema 包含不允许的外部引用。"
        return "工具参数的 Schema 无法安全校验。"
    except Exception:
        return "工具参数的 Schema 无法安全校验。"
    if error is None:
        return None
    path = format_validation_path(error)
    return f"工具参数不符合 Schema：{path or 'root'}"


__all__ = [
    "KNOWLEDGE_SEARCH_DEFINITION",
    "KNOWLEDGE_SEARCH_ID",
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
