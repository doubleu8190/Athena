"""配置和模型 Provider 查询用例。"""

from __future__ import annotations

from typing import Any

from domain.configuration import ProviderQueryPort, SettingsQueryPort
from application.common.responses import ProviderResponse, SettingsResponse


class ProviderQueryService:
    """查询 Provider 配置并转换为应用响应。
    """
    def __init__(self, port: ProviderQueryPort) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `port`（ProviderQueryPort）：本用例依赖的应用端口，提供所需外部能力；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._port = port

    async def list_providers(self) -> list[ProviderResponse]:
        """读取可供界面展示的 Provider 配置列表。

        参数：
        - 无参数。

        返回：
        - list[ProviderResponse]：按名称排列的 Provider 配置响应列表。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [ProviderResponse(**value) for value in await self._port.list_providers()]


class SettingsQueryService:
    """查询应用当前设置并转换为响应。
    """
    def __init__(self, port: SettingsQueryPort) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `port`（SettingsQueryPort）：本用例依赖的应用端口，提供所需外部能力；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._port = port

    async def get_settings(self) -> SettingsResponse:
        """读取当前应用设置的响应对象。

        参数：
        - 无参数。

        返回：
        - SettingsResponse：当前应用设置的响应对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return SettingsResponse(dict(await self._port.get_settings()))


__all__ = ["ProviderQueryService", "SettingsQueryService"]
