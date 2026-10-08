"""查询端口的兼容导出。

正式定义按配置、工具、记忆和检索职责放在对应领域包中。
"""

from domain.configuration.query_ports import ProviderQueryPort, SettingsQueryPort
from domain.memory.query_ports import MemoryQueryPort
from domain.retrieval.query_ports import RetrievalQueryPort
from domain.tools.query_ports import ToolUsageQueryPort

__all__ = [
    "MemoryQueryPort",
    "ProviderQueryPort",
    "RetrievalQueryPort",
    "SettingsQueryPort",
    "ToolUsageQueryPort",
]
