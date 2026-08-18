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
        ("list_files", "列出当前会话可访问的文件资产及其解析状态。", list_files, {"type": "object", "properties": {}}),
        ("get_file_info", "获取指定会话附件的公开元数据、解析状态、能力和解析统计。", get_file_info, None),
        ("read_file", "读取已解析文件的文本分块；可按 page/sheet/path 定位。图片仅返回 OCR 文本，不做视觉理解。", read_file, None),
        ("search_file", "在已解析文件文本中执行关键词和向量混合搜索；图片只能搜索 OCR 文本。", search_file, None),
        ("extract_table", "读取解析阶段缓存的表格数据，支持 CSV、PDF、Word 和 Excel；无表格时返回空列表。", extract_table, None),
        ("summarize_file", "对文件文本生成分层摘要并缓存；启用任务队列时仅返回 queued 任务信息。", summarize_file, None),
        ("analyze_file", "按任务分析文件；图片优先使用视觉模型，未启用视觉时使用 OCR 文本 fallback 并明确标注来源。", analyze_file, None),
        ("analyze_codebase", "返回代码项目结构、语言、符号和依赖概览；启用任务队列时仅返回 queued 任务信息。", analyze_codebase, None),
        ("find_symbol", "在代码附件的符号索引中按名称模糊查找类、函数等定义。", find_symbol, None),
        ("find_definition", "定位代码符号定义；当前等价于 find_symbol，返回匹配的定义位置。", find_definition, None),
        ("find_references", "查找指向指定代码符号的 incoming 引用关系。", find_references, None),
        ("get_call_graph", "获取指定代码符号的调用关系，direction 支持 incoming、outgoing 或 both。", get_call_graph, None),
    ]
    explicit = {
        "get_file_info": {"type": "object", "properties": {"file_id": {"type": "string", "description": "当前会话中的附件 ID。"}}, "required": ["file_id"]},
        "read_file": {"type": "object", "properties": {
            "file_id": {"type": "string", "description": "当前会话中的附件 ID。"},
            "locator": {"type": "object", "description": "可选定位过滤，如 {\"page\": 3}、{\"sheet\": \"Sheet1\"} 或 {\"path\": \"src/app.py\"}。"},
            "limit": {"type": "integer", "default": 10, "description": "返回分块数量，运行时限制为 1-50。"},
        }, "required": ["file_id"]},
        "search_file": {"type": "object", "properties": {
            "file_id": {"type": "string", "description": "当前会话中的附件 ID。"},
            "query": {"type": "string", "description": "要搜索的文本查询。"},
            "limit": {"type": "integer", "default": 10, "description": "返回结果数量，运行时限制为 1-50。"},
        }, "required": ["file_id", "query"]},
        "extract_table": {"type": "object", "properties": {"file_id": {"type": "string", "description": "当前会话中的 CSV、PDF、Word 或 Excel 附件 ID。"}}, "required": ["file_id"]},
        "summarize_file": {"type": "object", "properties": {
            "file_id": {"type": "string", "description": "当前会话中的附件 ID。"},
            "summary_type": {"type": "string", "default": "general", "description": "摘要类型标签，如 general、technical。"},
        }, "required": ["file_id"]},
        "analyze_file": {"type": "object", "properties": {
            "file_id": {"type": "string", "description": "当前会话中的附件 ID。"},
            "task": {"type": "string", "description": "分析任务描述，例如统计字段、解释图片 OCR 文本或描述图片内容。"},
        }, "required": ["file_id", "task"]},
        "analyze_codebase": {"type": "object", "properties": {"file_id": {"type": "string", "description": "代码文件附件 ID。"}}, "required": ["file_id"]},
        "find_symbol": {"type": "object", "properties": {
            "file_id": {"type": "string", "description": "代码文件附件 ID。"},
            "name": {"type": "string", "description": "要查找的类、函数或其他符号名称。"},
        }, "required": ["file_id", "name"]},
        "find_definition": {"type": "object", "properties": {
            "file_id": {"type": "string", "description": "代码文件附件 ID。"},
            "name": {"type": "string", "description": "要定位定义的符号名称。"},
        }, "required": ["file_id", "name"]},
        "find_references": {"type": "object", "properties": {
            "file_id": {"type": "string", "description": "代码文件附件 ID。"},
            "name": {"type": "string", "description": "要查找引用的符号名称。"},
        }, "required": ["file_id", "name"]},
        "get_call_graph": {"type": "object", "properties": {
            "file_id": {"type": "string", "description": "代码文件附件 ID。"},
            "symbol": {"type": "string", "description": "目标符号名称。"},
            "direction": {"type": "string", "default": "both", "description": "调用方向：outgoing、incoming 或 both。"},
        }, "required": ["file_id", "symbol"]},
    }
    for name, description, handler, params in definitions:
        manager.register_native(name=name, description=description, handler=handler,
                                parameters=params or explicit[name], risk_level=RiskLevel.LOW, require_approval=False)
