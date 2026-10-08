"""集成端口的兼容导出。

文件和检索索引端口归属 ``domain.files``；本模块仅保留尚未细分的外部
集成接口旧导入路径。
"""

from .llm_ports import LLMPort
from .sandbox_ports import SandboxPort
from domain.files.indexing_ports import GraphIndexerPort, VectorIndexerPort

# 迁移期兼容名称；正式文件索引端口位于 ``domain.files.indexing_ports``。
VectorIndexPort = VectorIndexerPort
GraphIndexPort = GraphIndexerPort

__all__ = ["GraphIndexPort", "GraphIndexerPort", "LLMPort", "SandboxPort", "VectorIndexPort", "VectorIndexerPort"]
