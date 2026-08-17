"""Agent-facing file capability tools."""

from __future__ import annotations

from typing import Any

from athena.core.files.context import get_context
from athena.core.files.runtime import FileIntelligenceRuntime
from athena.core.tools.manager import UnifiedToolManager
from athena.models.tool import RiskLevel
from athena.models.file import FileTaskType


def register_file_capabilities(manager: UnifiedToolManager, runtime: FileIntelligenceRuntime) -> None:
    async def list_files() -> Any:
        return await runtime.list_files(get_context().session_id)

    async def get_file_info(file_id: str) -> Any:
        return await runtime.get_file_info(get_context().session_id, file_id)

    async def read_file(file_id: str, locator: dict[str, Any] | None = None, limit: int = 10) -> Any:
        return await runtime.read_file(get_context().session_id, file_id, locator, limit)

    async def search_file(file_id: str, query: str, limit: int = 10) -> Any:
        return await runtime.search_file(get_context().session_id, file_id, query, limit)

    async def extract_table(file_id: str) -> Any:
        return await runtime.extract_table(get_context().session_id, file_id)

    async def summarize_file(file_id: str, summary_type: str = "general") -> Any:
        if runtime.task_queue_available:
            task = await runtime.enqueue_task(
                get_context().session_id, file_id, FileTaskType.FILE_SUMMARY,
                payload={"summary_type": summary_type},
            )
            return {"queued": True, "task": task.model_dump(mode="json")}
        return await runtime.summarize_file(get_context().session_id, file_id, summary_type)

    async def analyze_file(file_id: str, task: str) -> Any:
        return await runtime.analyze_file(get_context().session_id, file_id, task)

    async def analyze_codebase(file_id: str) -> Any:
        if runtime.task_queue_available:
            task = await runtime.enqueue_task(
                get_context().session_id, file_id, FileTaskType.CODE_ANALYSIS,
            )
            return {"queued": True, "task": task.model_dump(mode="json")}
        return await runtime.analyze_codebase(get_context().session_id, file_id)

    async def find_symbol(file_id: str, name: str) -> Any:
        return await runtime.find_symbol(get_context().session_id, file_id, name)

    async def find_definition(file_id: str, name: str) -> Any:
        return await runtime.find_symbol(get_context().session_id, file_id, name)

    async def find_references(file_id: str, name: str) -> Any:
        return await runtime.get_call_graph(get_context().session_id, file_id, name, "incoming")

    async def get_call_graph(file_id: str, symbol: str, direction: str = "both") -> Any:
        return await runtime.get_call_graph(get_context().session_id, file_id, symbol, direction)

    definitions = [
        ("list_files", "列出当前会话中的文件资产", list_files, {"type": "object", "properties": {}}),
        ("get_file_info", "获取会话附件元数据、解析状态和能力", get_file_info, None),
        ("read_file", "按 file_id 和定位信息读取文件内容", read_file, None),
        ("search_file", "在会话附件中按 query 搜索内容", search_file, None),
        ("extract_table", "提取 PDF、Word 或 Excel 中已解析的表格", extract_table, None),
        ("summarize_file", "对文件执行分层摘要", summarize_file, None),
        ("analyze_file", "按任务分析文档、数据或图片", analyze_file, None),
        ("analyze_codebase", "返回代码项目结构、语言、符号和依赖概览", analyze_codebase, None),
        ("find_symbol", "在代码项目中查找符号", find_symbol, None),
        ("find_definition", "定位代码符号定义", find_definition, None),
        ("find_references", "查找代码符号的引用关系", find_references, None),
        ("get_call_graph", "获取代码符号调用关系", get_call_graph, None),
    ]
    explicit = {
        "get_file_info": {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]},
        "read_file": {"type": "object", "properties": {"file_id": {"type": "string"}, "locator": {"type": "object"}, "limit": {"type": "integer", "default": 10}}, "required": ["file_id"]},
        "search_file": {"type": "object", "properties": {"file_id": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer", "default": 10}}, "required": ["file_id", "query"]},
        "extract_table": {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]},
        "summarize_file": {"type": "object", "properties": {"file_id": {"type": "string"}, "summary_type": {"type": "string", "default": "general"}}, "required": ["file_id"]},
        "analyze_file": {"type": "object", "properties": {"file_id": {"type": "string"}, "task": {"type": "string"}}, "required": ["file_id", "task"]},
        "analyze_codebase": {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]},
        "find_symbol": {"type": "object", "properties": {"file_id": {"type": "string"}, "name": {"type": "string"}}, "required": ["file_id", "name"]},
        "find_definition": {"type": "object", "properties": {"file_id": {"type": "string"}, "name": {"type": "string"}}, "required": ["file_id", "name"]},
        "find_references": {"type": "object", "properties": {"file_id": {"type": "string"}, "name": {"type": "string"}}, "required": ["file_id", "name"]},
        "get_call_graph": {"type": "object", "properties": {"file_id": {"type": "string"}, "symbol": {"type": "string"}, "direction": {"type": "string", "default": "both"}}, "required": ["file_id", "symbol"]},
    }
    for name, description, handler, params in definitions:
        manager.register_native(name=name, description=description, handler=handler,
                                parameters=params or explicit[name], risk_level=RiskLevel.LOW, require_approval=False)
