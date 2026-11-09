"""Unified Run tool catalog, authorization, and single-call dispatch."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC
from typing import Any, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from app.knowledge.service import (
    KnowledgeInputError,
    KnowledgeNotFoundError,
    KnowledgeSearchUnavailableError,
    KnowledgeService,
)
from app.memory.service import MemoryService
from app.project.knowledge import KnowledgeNotFoundError as ProjectKnowledgeNotFoundError
from app.project.knowledge import ProjectKnowledgeService
from common.date import utc_now
from common.json import utf8_size
from domain.auth.ports import CurrentSessionReader
from domain.knowledge.model import SearchResult
from domain.mcp import (
    McpCallResult,
    McpCallStatus,
    McpToolExecutor,
    McpToolRuntimeReader,
)
from domain.memory.model import AccessContext, RecallRequest
from domain.project import ProjectNotFoundError
from domain.session import SessionRepository, SessionScope
from domain.tool import (
    MAX_TOOL_ARGUMENT_BYTES,
    AuthorizedRunContext,
    BuiltinToolSource,
    McpToolSource,
    RuntimeToolDefinition,
    RuntimeToolSnapshot,
    RunToolRuntime,
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
        self._clock = clock

    async def authorize(
        self,
        owner_id: str,
        chat_session_id: str,
        run_id: str,
        auth_session_id: str | None,
    ) -> AuthorizedRunContext | None:
        del run_id
        if not auth_session_id:
            return None
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)
        user = await self._auth.get_current_session_user(owner_id, auth_session_id, now)
        if user is None or user.id != owner_id:
            return None
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
        """Freeze v1 clarification-only or the complete v2 tool catalog."""
        if prompt_template_version == "run-v1":
            return RuntimeToolSnapshot(tools=(REQUEST_USER_INPUT_DEFINITION,))
        if prompt_template_version != "run-v2":
            raise ValueError("run_prompt_template_unsupported")

        definitions: list[RuntimeToolDefinition] = [REQUEST_USER_INPUT_DEFINITION]
        if self._memory_enabled and self._memory is not None:
            definitions.append(MEMORY_SEARCH_DEFINITION)
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

    def validate_call(
        self,
        definition: RuntimeToolDefinition | None,
        model_name: str,
        arguments: dict[str, Any],
    ) -> ToolOutcome | None:
        """Validate frozen identity and local JSON Schema without network access."""
        if arguments and utf8_size(arguments) > MAX_TOOL_ARGUMENT_BYTES:
            return _error("tool_arguments_too_large", "工具参数超过上限。")
        if definition is None or definition.model_name != model_name:
            return _error("tool_unknown", "模型请求了冻结目录中不存在的工具。")
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
        validation_error = self.validate_call(definition, model_name, arguments)
        if validation_error is not None:
            return ToolExecutionResult(validation_error)
        assert definition is not None
        if definition.source.kind == "mcp":
            return await self._execute_mcp(
                definition,
                arguments,
                context,
                deadline_seconds,
                run_remaining_seconds,
            )
        return await self._execute_builtin(definition, arguments, context, deadline_seconds)

    async def _execute_mcp(
        self,
        definition: RuntimeToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        deadline_seconds: float,
        run_remaining_seconds: float,
    ) -> ToolExecutionResult:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        business_deadline = started_at + deadline_seconds
        run_deadline = started_at + max(0.0, run_remaining_seconds)
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
        result = await self._mcp_executor.call(
            target,
            dict(arguments),
            deadline_seconds=remaining,
        )
        outcome = _mcp_outcome(result)
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
        if source.version != BUILTIN_VERSION:
            return ToolExecutionResult(_error("tool_unavailable", "内置工具版本不受支持。"))
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
        if definition.model_name == "request_user_input":
            return ToolExecutionResult(
                _error("tool_unavailable", "澄清工具只能由 Run 执行器处理。")
            )
        if definition.model_name == "memory_search":
            outcome = await self._memory_search(before, arguments)
        elif definition.model_name == "knowledge_search":
            outcome = await self._knowledge_search(before, arguments, deadline_seconds)
        else:
            return ToolExecutionResult(_error("tool_unavailable", "内置工具处理器不可用。"))
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


def _bounded_limit(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8:
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
    "RunToolRuntimeService",
]
