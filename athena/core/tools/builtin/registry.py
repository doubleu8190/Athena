"""内置工具注册表 — 将默认工具注册到 UnifiedToolManager.

工具清单（参考技术规格 4.3 节）：
- read_file (low / 无审批)
- write_file (medium / 审批)
- list_directory (low / 无审批)
- exec_shell (high / 审批)
"""

from __future__ import annotations

from athena.core.tools.builtin.file_tools import list_directory, read_file, write_file
from athena.core.tools.builtin.shell_tools import exec_shell
from athena.core.tools.manager import UnifiedToolManager
from athena.models.tool import RiskLevel


def register_builtin_tools(manager: UnifiedToolManager) -> None:
    """注册所有内置 Native 工具到管理器."""
    manager.register_native(
        name="read_file",
        description="读取指定路径的文件文本内容",
        handler=read_file,
        risk_level=RiskLevel.LOW,
        require_approval=False,
    )
    manager.register_native(
        name="write_file",
        description=(
            "将文本内容写入指定文件。默认会完全覆盖（删除原有全部内容后写入新内容）。"
            "要生成完整文件时，请在一次调用中写入完整内容，不要分片多次覆盖同一文件；"
            "要保留原有内容并在末尾追加时，请显式设置 append=true。"
        ),
        handler=write_file,
        risk_level=RiskLevel.MEDIUM,
        require_approval=True,
    )
    manager.register_native(
        name="list_directory",
        description="列出指定目录下的文件和子目录",
        handler=list_directory,
        risk_level=RiskLevel.LOW,
        require_approval=False,
    )
    manager.register_native(
        name="exec_shell",
        description="在宿主机执行 Shell 命令（高风险，需用户审批）",
        handler=exec_shell,
        risk_level=RiskLevel.HIGH,
        require_approval=True,
    )
