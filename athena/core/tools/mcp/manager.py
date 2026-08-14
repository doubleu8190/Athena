"""MCP 服务器管理器 — 统一「持久化 + 连接 + 注册」.

职责：
- register_server: 持久化配置到 DB，连接 MCP Server 并注册其工具到 UnifiedToolManager
- unregister_server: 移除已注册工具 + 断开连接 + DB 软删除
- load_persisted: 启动时恢复所有已持久化的 MCP Server（单台失败不阻断）
- list_servers: 合并 DB 配置与实时连接状态（env 仅返回掩码）
"""

from __future__ import annotations

import os
from typing import Any, TYPE_CHECKING

from athena.core.tools.mcp.adapter import MCPToolAdapter
from athena.models.mcp import McpServerConfig
from athena.utils.logging import get_logger

if TYPE_CHECKING:
    from athena.db.database import Database
    from athena.core.tools.manager import UnifiedToolManager

logger = get_logger(__name__)

# npx -y 冷启动（下载包 + 启动进程）可能耗时较长，注册握手用宽松超时
REGISTER_CONNECT_TIMEOUT = 120.0


def _mask_env_value(value: str) -> str:
    """掩码环境变量值，仿 providers.py 的密钥掩码."""
    if not value:
        return ""
    return f"••••{value[-4:]}"


class MCPManager:
    """MCP 服务器管理器.

    Attributes:
        _servers: name -> {config, tool_names, error} 的实时注册状态
        _adapter: MCPToolAdapter 实例（连接 + 注册工具）
        _db: Database 实例（持久化）
    """

    def __init__(
        self,
        tool_manager: UnifiedToolManager,
        db: Database,
        adapter: MCPToolAdapter | None = None,
    ) -> None:
        self._tool_manager = tool_manager
        self._db = db
        self._adapter = adapter or MCPToolAdapter(tool_manager)
        self._servers: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 注册 / 注销
    # ------------------------------------------------------------------

    async def register_server(
        self, name: str, config: McpServerConfig
    ) -> dict[str, Any]:
        """注册一个 MCP Server：持久化配置 + 连接 + 注册工具.

        已存在同名服务器时先拆除旧连接与旧工具（覆盖重注册）。
        连接失败不抛出：配置仍持久化，返回 status=failed 供前端展示修复。

        Returns:
            {"name", "status": "connected"|"failed", "tool_count", "error"}
        """
        # 覆盖重注册：先拆除旧的实时状态
        if name in self._servers:
            await self._unregister_live(name)

        # 先持久化：即使连接失败也保留配置，重启后仍会重试
        await self._db.mcp_servers.upsert(name, config)

        server_command = [config.command, *config.args]
        try:
            tool_names = await self._adapter.register_server(
                server_name=name,
                server_command=server_command,
                env=config.env,
                connect_timeout=REGISTER_CONNECT_TIMEOUT,
            )
        except Exception as e:
            logger.error("mcp_register_failed", server=name, error=str(e))
            self._servers[name] = {
                "config": config,
                "tool_names": [],
                "error": str(e),
            }
            return {
                "name": name,
                "status": "failed",
                "tool_count": 0,
                "error": str(e),
            }

        self._servers[name] = {
            "config": config,
            "tool_names": tool_names,
            "error": None,
        }
        logger.info("mcp_server_registered", server=name, tool_count=len(tool_names))
        return {
            "name": name,
            "status": "connected",
            "tool_count": len(tool_names),
            "error": None,
        }

    async def _unregister_live(self, name: str) -> None:
        """拆除实时状态：移除已注册工具 + 断开客户端."""
        state = self._servers.pop(name, None)
        if state:
            for tool_name in state.get("tool_names", []):
                self._tool_manager.unregister(tool_name)
        await self._adapter.unregister_server(name)

    async def unregister_server(self, name: str) -> None:
        """注销服务器：拆除实时状态 + DB 软删除."""
        await self._unregister_live(name)
        await self._db.mcp_servers.soft_delete(name)
        logger.info("mcp_server_unregistered", server=name)

    # ------------------------------------------------------------------
    # 启动恢复
    # ------------------------------------------------------------------

    async def load_persisted(self) -> None:
        """启动时恢复所有已持久化的 MCP Server.

        单台失败仅记录日志并继续，不阻断应用启动。
        """
        rows = await self._db.mcp_servers.list_all()
        for row in rows:
            try:
                await self.register_server(row.name, row.config)
            except Exception as e:
                logger.warning(
                    "mcp_load_persisted_failed", server=row.name, error=str(e)
                )

    # ------------------------------------------------------------------
    # 查询 / 关闭
    # ------------------------------------------------------------------

    async def list_servers(self) -> list[dict[str, Any]]:
        """列出所有已持久化服务器，合并实时连接状态.

        每个条目：name / command / args / env_masked / status / tool_count /
        error / created_at。env 值绝不返回原文，仅掩码。
        """
        rows = await self._db.mcp_servers.list_all()
        items: list[dict[str, Any]] = []
        for row in rows:
            config = row.config
            state = self._servers.get(row.name)
            connected = state is not None and not state.get("error")
            items.append(
                {
                    "name": row.name,
                    "command": config.command,
                    "args": config.args,
                    "env_masked": {
                        k: _mask_env_value(v) for k, v in config.env.items()
                    },
                    "status": "connected" if connected else "failed",
                    "tool_count": len(state.get("tool_names", [])) if state else 0,
                    "error": state.get("error") if state else None,
                    "created_at": row.created_at.isoformat(),
                }
            )
        return items

    async def shutdown(self) -> None:
        """关闭所有 MCP 连接（应用退出清理，防子进程泄漏）."""
        self._servers.clear()
        await self._adapter.disconnect_all()


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_mcp_manager: MCPManager


def get_mcp_manager() -> MCPManager:
    """获取 MCP 管理器单例."""
    return _mcp_manager


def set_mcp_manager(manager: MCPManager) -> None:
    """设置 MCP 管理器单例（main.py 启动时注入）."""
    global _mcp_manager
    _mcp_manager = manager
