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
    NativeHandler,
    NativeTool,
    ToolProtocol,
    _infer_parameters,
)
from athena.models.tool import RiskLevel, ToolResult
from athena.utils.logging import get_logger

if TYPE_CHECKING:
    from athena.core.tools.mcp.client import MCPClient
    from athena.gateway.approval import ApprovalManager

logger = get_logger(__name__)


class UnifiedToolManager:
    """统一工具管理器."""

    def __init__(self, approval_manager: ApprovalManager | None = None) -> None:
        self._tools: dict[str, ToolProtocol] = {}
        self._approval_manager = approval_manager

    # ------------------------------------------------------------------
    # 注册接口
    # ------------------------------------------------------------------

    def register_native(
        self,
        name: str,
        description: str,
        handler: NativeHandler,
        parameters: dict[str, Any] | None = None,
        risk_level: RiskLevel | str = RiskLevel.LOW,
        require_approval: bool = False,
    ) -> None:
        """注册 Native 工具."""
        if name in self._tools:
            logger.warning("tool_already_registered", tool=name, action="overwrite")
        self._tools[name] = NativeTool(
            name=name,
            description=description,
            handler=handler,
            parameters=parameters,
            risk_level=risk_level,
            require_approval=require_approval,
        )
        logger.info("native_tool_registered", tool=name)

    def register_mcp_tools(
        self,
        server_name: str,
        tool_defs: list[dict[str, Any]],
        mcp_client: MCPClient,
    ) -> None:
        """注册 MCP 服务器的工具集.

        Args:
            server_name: MCP 服务器名称
            tool_defs: 工具定义列表，每项含
                name/description/parameters/remote_name/risk_level/require_approval
            mcp_client: MCP 客户端实例，需提供 async call_tool(name, params) 方法
        """
        for tool_def in tool_defs:
            name = tool_def["name"]
            if name in self._tools:
                logger.warning("tool_already_registered", tool=name, action="overwrite")
            self._tools[name] = MCPTool(
                name=name,
                description=tool_def.get("description", ""),
                parameters=tool_def.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
                mcp_client=mcp_client,
                server_name=server_name,
                remote_name=tool_def.get("remote_name"),
                risk_level=tool_def.get("risk_level", RiskLevel.MEDIUM),
                require_approval=tool_def.get("require_approval", True),
            )
            logger.info("mcp_tool_registered", tool=name, server=server_name)

    def unregister(self, name: str) -> None:
        """注销工具."""
        self._tools.pop(name, None)

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_tool(self, name: str) -> ToolProtocol | None:
        return self._tools.get(name)

    def list_tools(self) -> list[ToolProtocol]:
        return list(self._tools.values())

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    def require_approval(self, tool_name: str) -> bool:
        """检查工具是否需要审批."""
        tool = self._tools.get(tool_name)
        return bool(tool and tool.schema.require_approval)

    def get_risk_level(self, tool_name: str) -> str:
        """获取工具风险等级."""
        tool = self._tools.get(tool_name)
        return str(tool.schema.risk_level) if tool else "low"

    # ------------------------------------------------------------------
    # 调用接口
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        name: str,
        params: dict[str, Any],
        session_id: str | None = None,
        run_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> ToolResult:
        """统一工具调用入口，含审批检查.

        遵循 project_memory：审批通过 ApprovalManager.request_approval() 立即返回 Future，
        调用方 await future 等待结果，避免审批风暴。
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(status="failed", error=f"Tool '{name}' not registered")

        # 审批检查
        if tool.schema.require_approval and self._approval_manager is not None:
            try:
                request = await self._approval_manager.request_approval(
                    tool_name=name,
                    arguments=params,
                    risk_level=str(tool.schema.risk_level),
                    session_id=session_id or "",
                    run_id=run_id or "",
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

        # 执行工具
        return await tool.execute(**params)

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
            if names is not None and name not in names:
                continue
            result.append(self._build_structured_tool(tool))
        return result

    def _build_structured_tool(self, tool: ToolProtocol) -> StructuredTool:
        """构建单个 LangChain StructuredTool."""
        args_model = self._build_args_model(tool)
        schema = tool.schema

        async def _runner(**params: Any) -> str:
            result = await tool.execute(**params)
            if result.status == "success":
                return result.output or ""
            return f"[ERROR] {result.error or 'tool failed'}"

        return StructuredTool.from_function(
            coroutine=_runner,
            name=schema.name,
            description=schema.description,
            args_schema=args_model,
        )

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


# 全局单例
_tool_manager: UnifiedToolManager | None = None


def get_tool_manager(
    approval_manager: ApprovalManager | None = None,
) -> UnifiedToolManager:
    """获取工具管理器单例."""
    global _tool_manager
    if _tool_manager is None:
        _tool_manager = UnifiedToolManager(approval_manager=approval_manager)
    return _tool_manager


def set_tool_manager(manager: UnifiedToolManager) -> None:
    global _tool_manager
    _tool_manager = manager


def reset_tool_manager() -> None:
    """重置单例（测试用）."""
    global _tool_manager
    _tool_manager = None
