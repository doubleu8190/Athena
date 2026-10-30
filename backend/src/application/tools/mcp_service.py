"""MCP 服务端管理应用服务。"""

from __future__ import annotations

from domain.tools import MCPPort, MCPServerConfig
from .contracts import McpServerConfigInput
from application.common.responses import McpServerResponse


class MCPService:
    """协调 MCP 服务端注册、注销、查询和关闭。"""

    def __init__(self, port: MCPPort) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `port`（MCPPort）：本用例依赖的应用端口，提供所需外部能力；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._port = port

    async def register(self, name: str, config: McpServerConfigInput | MCPServerConfig) -> dict[str, object]:
        """注册 MCP 服务端。

        参数：
        - `name`（str）：目标名称，用于标识相应配置、工具或对象；约束：无额外运行时限制。
        - `config`（McpServerConfigInput | MCPServerConfig）：当前操作使用的已解析配置；约束：无额外运行时限制。

        返回：
        - dict[str, object]：包含新注册 MCP 服务标识和运行状态的字段映射。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        domain_config = config if isinstance(config, MCPServerConfig) else MCPServerConfig(
            config.command, config.args, dict(config.env), config.image_id, config.network_policy, config.enabled
        )
        return await self._port.register(name, domain_config)

    async def unregister(self, name: str) -> None:
        """注销 MCP 服务端。

        参数：
        - `name`（str）：目标名称，用于标识相应配置、工具或对象；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        await self._port.unregister(name)

    async def list_servers(self) -> list[McpServerResponse]:
        """列出 MCP 服务端状态。

        参数：
        - 无参数。

        返回：
        - list[McpServerResponse]：当前可见的 MCP 服务配置响应列表。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [McpServerResponse(
            name=str(value.get("name", value.get("server_name", ""))),
            config=dict(value.get("config") or {}),
            status=str(value.get("status")) if value.get("status") is not None else None,
        ) for value in await self._port.list_servers()]

    async def shutdown(self) -> None:
        """关闭全部 MCP 连接。

        参数：
        - 无参数。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        await self._port.shutdown()


__all__ = ["MCPService"]
