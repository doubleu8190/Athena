"""任务理解契约的兼容导出。

具体模型位于 ``specs.py``，本模块保留迁移期的旧导入路径。
"""

from .specs import ContextRequirement, OutputSpec, QueryHints, TaskMode, UserTaskSpec

__all__ = ["ContextRequirement", "OutputSpec", "QueryHints", "TaskMode", "UserTaskSpec"]
