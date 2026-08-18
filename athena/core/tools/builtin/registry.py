"""内置工具注册表 — 将默认工具注册到 UnifiedToolManager.

工具清单（参考技术规格 4.3 节）：
- read_local_file (low / 无审批)
- write_file (medium / 审批)
- list_directory (low / 无审批)
- exec_shell (high / 审批)
- get_local_file_info (low / 无审批) — 文件元数据侦察
- read_local_file_section (low / 无审批) — 精准读取指定行范围
- search_local_file (low / 无审批) — 正则搜索定位
- read_local_file_full (low / 无审批) — 安全全量读取（带 50KB 阀门）

注册函数只负责写入运行时管理器，持久化治理由 ToolCatalogService 统一处理。
"""

from __future__ import annotations

from typing import TypedDict

from athena.core.tools.builtin.file_tools import (
    get_file_info,
    list_directory,
    read_file,
    read_file_section,
    read_full_file,
    search_in_file,
    write_file,
)
from athena.core.tools.builtin.shell_tools import exec_shell
from athena.core.tools.base import NativeHandler
from athena.core.tools.manager import UnifiedToolManager
from athena.core.tools.spec import ToolSpec
from athena.models.tool import RiskLevel

class BuiltinToolDefinition(TypedDict):
    name: str
    description: str
    handler: NativeHandler
    risk_level: RiskLevel
    require_approval: bool


# 内置工具的默认定义（注册名 / 描述 / handler / 默认治理参数）
_BUILTIN_TOOLS: list[BuiltinToolDefinition] = [
    {
        "name": "read_local_file",
        "description": "读取指定路径的文件文本内容",
        "handler": read_file,
        "risk_level": RiskLevel.LOW,
        "require_approval": False,
    },
    {
        "name": "write_file",
        "description": (
            "将文本内容写入指定文件。默认会完全覆盖（删除原有全部内容后写入新内容）。"
            "要生成完整文件时，请在一次调用中写入完整内容，不要分片多次覆盖同一文件；"
            "要保留原有内容并在末尾追加时，请显式设置 append=true。"
        ),
        "handler": write_file,
        "risk_level": RiskLevel.MEDIUM,
        "require_approval": True,
    },
    {
        "name": "list_directory",
        "description": "列出指定目录下的文件和子目录",
        "handler": list_directory,
        "risk_level": RiskLevel.LOW,
        "require_approval": False,
    },
    {
        "name": "exec_shell",
        "description": "在宿主机执行 Shell 命令（高风险，需用户审批）",
        "handler": exec_shell,
        "risk_level": RiskLevel.HIGH,
        "require_approval": True,
    },
    {
        "name": "get_local_file_info",
        "description": (
            "返回文件的元数据（大小、行数、是否二进制、文件类型）。"
            "用于 Agent 侦察文件特征，决定下一步策略（直接读还是分段搜）。"
        ),
        "handler": get_file_info,
        "risk_level": RiskLevel.LOW,
        "require_approval": False,
    },
    {
        "name": "read_local_file_section",
        "description": (
            "读取指定文件从 start_line 开始的 limit 行（最大 200 行）。"
            "当知道具体函数或报错行位置时，精准读取局部代码。"
        ),
        "handler": read_file_section,
        "risk_level": RiskLevel.LOW,
        "require_approval": False,
    },
    {
        "name": "search_local_file",
        "description": (
            "在文件中执行正则匹配，返回匹配行及其行号。"
            "用于定位函数定义、异常抛出、特定模式等。"
        ),
        "handler": search_in_file,
        "risk_level": RiskLevel.LOW,
        "require_approval": False,
    },
    {
        "name": "read_local_file_full",
        "description": (
            "读取文件全量内容（仅当文件 < 50KB 时）。"
            "文件过大时返回错误，建议使用 search_in_file 定位后分段读取。"
        ),
        "handler": read_full_file,
        "risk_level": RiskLevel.LOW,
        "require_approval": False,
    },
]


def register_builtin_tools(
    manager: UnifiedToolManager,
) -> list[str]:
    """Register built-ins and return the names installed into the manager."""
    for tool_def in _BUILTIN_TOOLS:
        manager.register(
            ToolSpec(
                name=tool_def["name"],
                description=tool_def["description"],
                handler=tool_def["handler"],
                risk_level=tool_def["risk_level"],
                require_approval=tool_def["require_approval"],
            )
        )

    return [item["name"] for item in _BUILTIN_TOOLS]
