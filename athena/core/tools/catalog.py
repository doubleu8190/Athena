"""工具注册与持久化治理协调。

提供工具配置的运行时与持久化层之间的协调机制：

- ``ToolCatalogService`` — 工具目录服务，负责将运行时工具配置同步到数据库，
  并从数据库恢复用户自定义的治理参数（风险等级、审批要求、启用状态）。
- ``ToolRegistry`` — 工具注册器，将声明式工具规范安装到运行时管理器，
  并触发目录服务的持久化协调。
"""

from __future__ import annotations

from typing import Any, Protocol

from athena.core.tools.manager import UnifiedToolManager
from athena.core.tools.spec import ToolSpec


class ToolConfigRepository(Protocol):
    """工具配置持久化仓库协议。

    定义工具治理参数的 CRUD 接口，由 SQLite Repository 实现。
    """

    async def get(self, tool_name: str) -> Any:
        """获取单个工具配置。

        参数：
            tool_name: 工具名称。

        返回值：
            工具配置对象，不存在时返回 ``None``。
        """
        ...

    async def upsert(
        self,
        tool_name: str,
        execution_mode: str,
        server_name: str | None = None,
        remote_name: str | None = None,
        description: str = "",
        parameters: dict[str, Any] | None = None,
        risk_level: str = "medium",
        require_approval: bool = True,
        enabled: bool = True,
    ) -> None:
        """插入或更新工具配置（已存在时保留用户治理参数）。

        参数：
            tool_name: 工具名称（主键）。
            execution_mode: 执行模式（``"native"`` / ``"mcp"``）。
            server_name: MCP 服务器名称（仅 MCP 工具）。
            remote_name: 远程工具名称（仅 MCP 工具）。
            description: 工具描述。
            parameters: JSON Schema 参数定义。
            risk_level: 风险等级。
            require_approval: 是否需要审批。
            enabled: 是否启用。
        """
        ...

    async def update(
        self,
        tool_name: str,
        risk_level: str | None = None,
        require_approval: bool | None = None,
        enabled: bool | None = None,
    ) -> None:
        """更新工具治理参数（仅更新传入的字段）。

        参数：
            tool_name: 工具名称。
            risk_level: 新的风险等级。
            require_approval: 新的审批要求。
            enabled: 新的启用状态。
        """
        ...


class ToolCatalogService:
    """工具目录服务 — 运行时与持久化层的协调中枢。

    负责：
    1. 将运行时工具配置同步到数据库（``reconcile``）。
    2. 从数据库恢复用户自定义的治理参数。
    3. 处理用户通过 API 修改的治理参数（``update_governance``）。

    参数：
        repository: 工具配置持久化仓库。
    """

    def __init__(self, repository: ToolConfigRepository) -> None:
        """初始化当前对象。

        参数：
            repository (ToolConfigRepository): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self._repository = repository

    async def reconcile(
        self,
        manager: UnifiedToolManager,
        names: list[str] | None = None,
        registrations: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """协调运行时工具配置与数据库持久化。

        遍历管理器中的工具，将配置写入数据库，然后从数据库读取
        用户自定义的治理参数回写到运行时。

        参数：
            manager: 统一工具管理器。
            names: 要协调的工具名称列表；``None`` 表示协调所有工具。
            registrations: 附加注册信息（server_name/remote_name），
                键为工具名称。
        """
        selected = set(names) if names is not None else None
        registrations = registrations or {}
        for tool in manager.list_tools():
            schema = tool.schema
            if selected is not None and schema.name not in selected:
                continue
            registration = registrations.get(schema.name, {})
            # 将运行时配置写入数据库（已存在时保留用户治理参数）
            await self._repository.upsert(
                tool_name=schema.name,
                execution_mode=schema.execution_mode.value,
                server_name=registration.get("server_name"),
                remote_name=registration.get("remote_name"),
                description=schema.description,
                parameters=schema.parameters,
                risk_level=schema.risk_level.value,
                require_approval=schema.require_approval,
                enabled=manager.is_enabled(schema.name),
            )
            # 从数据库读取用户自定义的治理参数回写运行时
            persisted = await self._repository.get(schema.name)
            if persisted is not None:
                manager.update_tool_config(
                    schema.name,
                    risk_level=persisted.risk_level.value,
                    require_approval=persisted.require_approval,
                    enabled=persisted.enabled,
                )

    async def update_governance(
        self,
        manager: UnifiedToolManager,
        name: str,
        risk_level: str | None = None,
        require_approval: bool | None = None,
        enabled: bool | None = None,
    ) -> bool:
        """更新工具治理参数（同时更新运行时和持久化层）。

        参数：
            manager: 统一工具管理器。
            name: 工具名称。
            risk_level: 新的风险等级。
            require_approval: 新的审批要求。
            enabled: 新的启用状态。

        返回值：
            是否成功（工具必须已注册）。
        """
        if not manager.update_tool_config(
            name,
            risk_level=risk_level,
            require_approval=require_approval,
            enabled=enabled,
        ):
            return False
        await self._repository.update(
            name,
            risk_level=risk_level,
            require_approval=require_approval,
            enabled=enabled,
        )
        return True


class ToolRegistry:
    """工具注册器 — 将声明式工具规范安装到运行时管理器。

    参数：
        manager: 统一工具管理器。
        catalog: 工具目录服务，用于持久化协调。
    """

    def __init__(
        self,
        manager: UnifiedToolManager,
        catalog: ToolCatalogService,
    ) -> None:
        """初始化当前对象。

        参数：
            manager (UnifiedToolManager): 输入参数；其类型和取值约束由方法签名及实现定义。
            catalog (ToolCatalogService): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self.manager = manager
        self.catalog = catalog

    async def install(self, specs: list[ToolSpec]) -> None:
        """安装工具规范列表到运行时管理器。

        注册完成后触发目录服务的持久化协调。

        参数：
            specs: 工具规范列表。
        """
        names: list[str] = []
        for spec in specs:
            self.manager.register(spec)
            names.append(spec.name)
        await self.catalog.reconcile(self.manager, names=names)
