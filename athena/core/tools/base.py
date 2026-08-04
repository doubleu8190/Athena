"""工具基类 — Native 与 MCP 工具的统一抽象.

管理层统一（MCP 风格 Schema），执行层分离（Native / MCP）。
- NativeTool: 直接 Python 函数调用，适合高频轻量操作
- MCPTool: 通过 MCP 协议远程代理，适合外部服务/重型计算
"""

from __future__ import annotations

import inspect
import json
from typing import Any, TYPE_CHECKING, Awaitable, Callable, Protocol, runtime_checkable

from athena.models.tool import RiskLevel, ToolExecutionMode, ToolResult, ToolSchema
from athena.utils.logging import get_logger

if TYPE_CHECKING:
    from athena.core.tools.mcp.client import MCPClient

logger = get_logger(__name__)

# Native 工具处理器签名：接收任意关键字参数，返回字符串或可序列化对象
NativeHandler = Callable[..., Awaitable[Any]]


def _infer_parameters(handler: NativeHandler) -> dict[str, Any]:
    """从函数签名推断 JSON Schema 参数定义.

    遵循 project_memory 约束：使用 inspect 解析而非 type() 动态创建 Pydantic 模型，
    避免非注解属性错误。
    """
    sig = inspect.signature(handler)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue

        annotation = param.annotation
        type_map = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            list: "array",
            dict: "object",
        }
        json_type = type_map.get(annotation if annotation is not inspect.Parameter.empty else str, "string")

        prop: dict[str, Any] = {"type": json_type}
        if param.default is not inspect.Parameter.empty:
            prop["default"] = param.default
        else:
            required.append(name)

        properties[name] = prop

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


@runtime_checkable
class ToolProtocol(Protocol):
    """统一工具协议."""

    schema: ToolSchema

    async def execute(self, **params: Any) -> ToolResult: ...


class NativeTool:
    """Native 工具 — 直接调用 Python 异步函数."""

    def __init__(
        self,
        name: str,
        description: str,
        handler: NativeHandler,
        parameters: dict[str, Any] | None = None,
        risk_level: RiskLevel | str = RiskLevel.LOW,
        require_approval: bool = False,
    ) -> None:
        self.schema = ToolSchema(
            name=name,
            description=description,
            parameters=parameters or _infer_parameters(handler),
            require_approval=require_approval,
            risk_level=RiskLevel(risk_level) if isinstance(risk_level, str) else risk_level,
            execution_mode=ToolExecutionMode.NATIVE,
        )
        self._handler = handler

    async def execute(self, **params: Any) -> ToolResult:
        """执行工具调用."""
        try:
            result = await self._handler(**params)
            output = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
            return ToolResult(status="success", output=output)
        except Exception as e:
            logger.error("native_tool_failed", tool=self.schema.name, error=str(e))
            return ToolResult(status="failed", error=str(e))


class MCPTool:
    """MCP 工具 — 通过 MCP 客户端远程代理执行."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        mcp_client: MCPClient,
        server_name: str,
        remote_name: str | None = None,
        risk_level: RiskLevel | str = RiskLevel.MEDIUM,
        require_approval: bool = True,
    ) -> None:
        self.schema = ToolSchema(
            name=name,
            description=description,
            parameters=parameters,
            require_approval=require_approval,
            risk_level=RiskLevel(risk_level) if isinstance(risk_level, str) else risk_level,
            execution_mode=ToolExecutionMode.MCP,
        )
        self._mcp_client = mcp_client
        self._server_name = server_name
        # 本地注册名（可能带前缀）与远端 MCP 工具名分离，避免前缀污染远程调用
        self._remote_name = remote_name or name

    async def execute(self, **params: Any) -> ToolResult:
        """通过 MCP 协议调用远程工具.

        遵循 project_memory 约束：必须检查 result.get("isError") 判断成功状态。
        """
        try:
            result = await self._mcp_client.call_tool(self._remote_name, params)

            # MCP 响应可能为 dict 或带 content 的对象
            if isinstance(result, dict):
                is_error = result.get("isError", False)
                content = result.get("content", "")
                if isinstance(content, list):
                    text_parts = [
                        c.get("text", "") if isinstance(c, dict) else str(c)
                        for c in content
                    ]
                    output = "\n".join(text_parts)
                else:
                    output = str(content)
            else:
                # 对象式 MCP 响应
                is_error = getattr(result, "isError", False)
                output = str(getattr(result, "content", result))

            if is_error:
                logger.warning("mcp_tool_error", tool=self.schema.name, server=self._server_name)
                return ToolResult(status="failed", error=output)

            return ToolResult(status="success", output=output)
        except Exception as e:
            logger.error("mcp_tool_failed", tool=self.schema.name, error=str(e))
            return ToolResult(status="failed", error=str(e))


def build_langchain_tool_schema(tool: ToolProtocol) -> dict[str, Any]:
    """将 ToolSchema 转换为 LangChain bind_tools 兼容的字典描述."""
    return {
        "name": tool.schema.name,
        "description": tool.schema.description,
        "parameters": tool.schema.parameters,
    }
