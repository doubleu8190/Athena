"""统一工具管理器 — 注册 Native/MCP 工具，统一 Schema 与调用入口.

- register_native: 注册本地 Python 异步函数
- register_mcp_tools: 注册 MCP 服务器的工具集
- call_tool: 统一调用入口，含审批检查
- get_langchain_tools: 转换为 LangChain StructuredTool 供 bind_tools 使用
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from langchain_core.tools import StructuredTool
from pydantic import create_model

from athena.core.tools.base import (
    MCPTool,
    NativeTool,
    ToolProtocol,
)
from athena.models.tool import RiskLevel, ToolResult, ToolSchema
from athena.core.tools.spec import ApprovalPort
from athena.utils.logging import get_logger

if TYPE_CHECKING:
    from athena.core.tools.mcp.client import MCPClient
    from athena.core.tools.spec import ToolSpec

logger = get_logger(__name__)


class UnifiedToolManager:
    """统一工具管理器."""

    def __init__(self, approval_manager: ApprovalPort) -> None:
        """初始化当前对象。

        参数：
            approval_manager (审批Port): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self._tools: dict[str, ToolProtocol] = {}
        self._disabled: set[str] = set()
        self._approval_manager = approval_manager

    # ------------------------------------------------------------------
    # 注册接口
    # ------------------------------------------------------------------
    def register(
        self,
        spec: ToolSpec
    ) -> None:
        """注册 Native 工具."""
        if spec.name in self._tools:
            logger.warning("tool_already_registered", tool=spec.name, action="overwrite")
        self._tools[spec.name] = NativeTool(
            name=spec.name,
            description=spec.description,
            handler=spec.handler,
            parameters=spec.parameters,
            risk_level=spec.risk_level,
            require_approval=spec.require_approval,
        )
        logger.info("native_tool_registered", tool=spec.name)
        self.set_enabled(spec.name, spec.enabled)

    def register_mcp_tools(
        self,
        tools: list[MCPTool],
    ) -> None:
        """注册 MCP 服务器的工具集.

        参数：
            server_name: MCP 服务器名称
            tool_defs: 工具定义列表，每项含
                name/description/parameters/remote_name/risk_level/require_approval
            mcp_client: MCP 客户端实例，需提供 async call_tool(name, params) 方法
        """
        for tool in tools:
            name = tool.schema.name
            if name in self._tools:
                logger.warning("tool_already_registered", tool=name, action="overwrite")
            self._tools[name] = tool
            logger.info("mcp_tool_registered", tool=name, server=tool._server_name)

    def unregister(self, name: str) -> None:
        """注销工具."""
        self._tools.pop(name, None)

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_tool(self, name: str) -> ToolProtocol | None:
        """执行“get tool”操作。

        参数：
            name (str): 资源名称或稳定标识。

        返回值：
            ToolProtocol | None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return self._tools.get(name)

    def contains(self, name: str) -> bool:
        """执行“contains”操作。

        参数：
            name (str): 资源名称或稳定标识。

        返回值：
            bool: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return name in self._tools

    def list_tools(self) -> list[ToolProtocol]:
        """执行“list tools”操作。

        返回值：
            list[ToolProtocol]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return list(self._tools.values())

    def list_names(self) -> list[str]:
        """执行“list names”操作。

        返回值：
            list[str]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return list(self._tools.keys())

    def is_enabled(self, name: str) -> bool:
        """工具是否启用（未注册视为不可用）."""
        return name in self._tools and name not in self._disabled

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """启用/停用工具. 未注册返回 False."""
        if name not in self._tools:
            return False
        if enabled:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)
        logger.info("tool_enablement_changed", tool=name, enabled=enabled)
        return True

    def require_approval(self, tool_name: str) -> bool:
        """检查工具是否需要审批."""
        tool = self._tools.get(tool_name)
        return bool(tool and tool.schema.require_approval)

    def get_risk_level(self, tool_name: str) -> str:
        """获取工具风险等级."""
        tool = self._tools.get(tool_name)
        return str(tool.schema.risk_level) if tool else "low"

    def update_tool_config(
        self,
        name: str,
        risk_level: str | None = None,
        require_approval: bool | None = None,
        enabled: bool | None = None,
    ) -> bool:
        """更新工具治理参数（内存）.

        同步更新 ToolSchema 字段和 _disabled 集合。
        DB 写入由调用方负责，保持职责分离。

        返回值：
            是否成功（工具必须已注册）
        """
        tool = self._tools.get(name)
        if tool is None:
            return False

        if risk_level is not None:
            tool.schema.risk_level = RiskLevel(risk_level)
        if require_approval is not None:
            tool.schema.require_approval = require_approval
        if enabled is not None:
            if enabled:
                self._disabled.discard(name)
            else:
                self._disabled.add(name)

        logger.info(
            "tool_config_updated",
            tool=name,
            risk_level=risk_level,
            require_approval=require_approval,
            enabled=enabled,
        )
        return True

    # ------------------------------------------------------------------
    # 调用接口
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        name: str,
        params: dict[str, Any],
        session_id: str,
        run_id: str,
        tool_call_id: str,
    ) -> ToolResult:
        """统一工具调用入口，含审批检查.

        遵循 project_memory：审批通过 审批Manager.request_approval() 立即返回 Future，
        调用方 await future 等待结果，避免审批风暴。

        session_id / run_id / tool_call_id 为必填：标识本次工具调用归属的会话、运行与
        具体工具调用，用于审批留痕与子代理父链上下文。
        """
        # 每次调用拥有独立的参数映射。并发会话可能调用同一个 NativeTool，
        # 因此上下文字段绝不能在会话之间泄漏。
        params = dict(params)
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(status="failed", error=f"Tool '{name}' not registered")

        # 停用检查
        if name in self._disabled:
            logger.info("tool_call_disabled", tool=name, session_id=session_id)
            return ToolResult(status="failed", error=f"Tool '{name}' is disabled")

        # 审批检查
        if tool.schema.require_approval:
            try:
                request = await self._approval_manager.request_approval(
                    tool_name=name,
                    arguments=params,
                    risk_level=str(tool.schema.risk_level),
                    session_id=session_id,
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                )
                approved = await request.future
                if not approved:
                    logger.info("tool_call_denied", tool=name, session_id=session_id)
                    return ToolResult(
                        status="denied",
                        output="用户拒绝了该操作",
                    )
            except Exception as e:
                logger.error("approval_failed", tool=name, error=str(e))
                return ToolResult(status="failed", error=f"审批流程异常: {e}")

        # 所有本地能力都从协程本地上下文读取可信的调用元数据，
        # 该元数据不会暴露为模型参数。
        token = None
        if isinstance(tool, NativeTool):
            from athena.core.tools.spec import ToolContext, set_tool_context

            context = ToolContext(session_id, run_id, tool_call_id)
            token = set_tool_context(context)
        try:
            return await tool.execute(**params)
        finally:
            if token is not None:
                from athena.core.tools.spec import reset_tool_context

                reset_tool_context(token)

    # ------------------------------------------------------------------
    # LangChain 集成
    # ------------------------------------------------------------------

    def get_langchain_tools(
        self, names: list[str] | None = None
    ) -> list[StructuredTool]:
        """转换为 LangChain StructuredTool 列表，用于 bind_tools.

        遵循 project_memory：使用 Pydantic v2 create_model() 动态构建参数模型，
        而非 type()，以避免 'non-annotated attribute' 错误。
        """
        result: list[StructuredTool] = []
        for name, tool in self._tools.items():
            if name in self._disabled:
                continue
            if names is not None and name not in names:
                continue
            result.append(self._build_structured_tool(tool))
        return result

    def _build_structured_tool(self, tool: ToolProtocol) -> StructuredTool:
        """构建单个 LangChain StructuredTool."""
        args_model = self._build_args_model(tool)
        schema = tool.schema

        async def _runner(**params: Any) -> str:
            """执行“runner”操作。

            参数：
                params (Any): 输入参数；其类型和取值约束由方法签名及实现定义。

            返回值：
                str: 操作结果；具体语义由调用场景决定。

            异常：
                Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
            """
            from athena.core.tools.spec import get_tool_context

            context = get_tool_context()
            params.pop("__placeholder__", None)
            result = await self.call_tool(
                schema.name,
                params,
                session_id=context.session_id,
                run_id=context.run_id,
                tool_call_id=context.tool_call_id,
            )
            if result.status == "success":
                return result.output or ""
            return f"[ERROR] {result.error or 'tool failed'}"

        return StructuredTool.from_function(
            coroutine=_runner,
            name=schema.name,
            description=self._describe_tool(schema),
            args_schema=args_model,
        )

    @staticmethod
    def _describe_tool(schema: ToolSchema) -> str:
        """拼接工具描述与治理信息（是否需要审批 + 风险等级）.

        治理信息并入 bind_tools 的函数描述，由函数 schema 注入给模型，
        系统提示词不再重复罗列工具列表。
        """
        flags: list[str] = []
        if schema.require_approval:
            flags.append("requires approval")
        flags.append(f"risk: {schema.risk_level.value}")
        flag_str = f" ({', '.join(flags)})" if flags else ""
        return f"{schema.description}{flag_str}"

    def _build_args_model(self, tool: ToolProtocol) -> type:
        """根据工具 parameters JSON Schema 构建 Pydantic 模型."""
        params = tool.schema.parameters or {}
        properties = params.get("properties", {})
        required = set(params.get("required", []))

        fields: dict[str, Any] = {}
        for field_name, spec in properties.items():
            json_type = spec.get("type", "string")
            py_type = {
                "string": str,
                "integer": int,
                "number": float,
                "boolean": bool,
                "array": list,
                "object": dict,
            }.get(json_type, str)

            if field_name in required:
                fields[field_name] = (py_type, ...)
            else:
                default = spec.get("default")
                fields[field_name] = (py_type, default)

        # 至少留一个占位字段，避免空模型报错
        if not fields:
            fields["__placeholder__"] = (str, None)

        return create_model(f"{tool.schema.name}Args", **fields)
