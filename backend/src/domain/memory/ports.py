"""记忆端口的兼容导出。

正式接口按记录、检索、写入和任务职责拆分；此模块保留旧导入路径。
"""

from .job_ports import MemoryJobPort
from .repository_ports import MemoryPort, MemoryRepositoryPort
from .retrieval_ports import MemoryVectorSearchPort
from .write_ports import MemoryFactExtractorPort, MemoryResolverPort, MemoryWriteTriggerPort

__all__ = [
    "MemoryFactExtractorPort",
    "MemoryJobPort",
    "MemoryPort",
    "MemoryRepositoryPort",
    "MemoryResolverPort",
    "MemoryVectorSearchPort",
    "MemoryWriteTriggerPort",
]
