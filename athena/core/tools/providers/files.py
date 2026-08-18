"""File intelligence tool declarations."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from athena.core.tools.spec import ToolSpec, get_tool_context
from athena.models.file import FileTaskType

if TYPE_CHECKING:
    from athena.core.files.runtime import FileIntelligenceRuntime


def build_file_tool_specs(runtime: FileIntelligenceRuntime) -> list[ToolSpec]:
    def session_id() -> str:
        return get_tool_context().session_id

    async def list_files() -> Any:
        return await runtime.list_files(session_id())

    async def get_file_info(file_id: str) -> Any:
        return await runtime.get_file_info(session_id(), file_id)

    async def read_file(
        file_id: str, locator: dict[str, Any] | None = None, limit: int = 10
    ) -> Any:
        return await runtime.read_file(session_id(), file_id, locator, limit)

    async def search_file(file_id: str, query: str, limit: int = 10) -> Any:
        return await runtime.search_file(session_id(), file_id, query, limit)

    async def extract_table(file_id: str) -> Any:
        return await runtime.extract_table(session_id(), file_id)

    async def summarize_file(file_id: str, summary_type: str = "general") -> Any:
        if runtime.task_queue_available:
            task = await runtime.enqueue_task(
                session_id(), file_id, FileTaskType.FILE_SUMMARY,
                payload={"summary_type": summary_type},
            )
            return {"queued": True, "task": task.model_dump(mode="json")}
        return await runtime.summarize_file(session_id(), file_id, summary_type)

    async def analyze_file(file_id: str, task: str) -> Any:
        return await runtime.analyze_file(session_id(), file_id, task)

    async def analyze_codebase(file_id: str) -> Any:
        if runtime.task_queue_available:
            task = await runtime.enqueue_task(
                session_id(), file_id, FileTaskType.CODE_ANALYSIS,
            )
            return {"queued": True, "task": task.model_dump(mode="json")}
        return await runtime.analyze_codebase(session_id(), file_id)

    async def find_symbol(file_id: str, name: str) -> Any:
        return await runtime.find_symbol(session_id(), file_id, name)

    async def find_definition(file_id: str, name: str) -> Any:
        return await runtime.find_symbol(session_id(), file_id, name)

    async def find_references(file_id: str, name: str) -> Any:
        return await runtime.get_call_graph(session_id(), file_id, name, "incoming")

    async def get_call_graph(
        file_id: str, symbol: str, direction: str = "both"
    ) -> Any:
        return await runtime.get_call_graph(session_id(), file_id, symbol, direction)

    file_id = {
        "file_id": {"type": "string", "description": "Current session attachment ID."}
    }
    definitions: list[tuple[str, str, Any, dict[str, Any]]] = [
        ("list_files", "列出当前会话可访问的文件资产及其解析状态。", list_files,
         {"type": "object", "properties": {}}),
        ("get_file_info", "获取指定会话附件的公开元数据、解析状态、能力和解析统计。", get_file_info,
         {"type": "object", "properties": file_id, "required": ["file_id"]}),
        ("read_file", "读取已解析文件的文本分块；可按 page/sheet/path 定位。", read_file,
         {"type": "object", "properties": {**file_id,
          "locator": {"type": "object", "description": "Optional page/sheet/path locator."},
          "limit": {"type": "integer", "default": 10}}, "required": ["file_id"]}),
        ("search_file", "在已解析文件文本中执行关键词和向量混合搜索。", search_file,
         {"type": "object", "properties": {**file_id,
          "query": {"type": "string"}, "limit": {"type": "integer", "default": 10}},
          "required": ["file_id", "query"]}),
        ("extract_table", "读取解析阶段缓存的表格数据。", extract_table,
         {"type": "object", "properties": file_id, "required": ["file_id"]}),
        ("summarize_file", "生成并缓存文件摘要。", summarize_file,
         {"type": "object", "properties": {**file_id,
          "summary_type": {"type": "string", "default": "general"}}, "required": ["file_id"]}),
        ("analyze_file", "按任务分析文件。", analyze_file,
         {"type": "object", "properties": {**file_id, "task": {"type": "string"}},
          "required": ["file_id", "task"]}),
        ("analyze_codebase", "返回代码项目结构、语言、符号和依赖概览。", analyze_codebase,
         {"type": "object", "properties": file_id, "required": ["file_id"]}),
        ("find_symbol", "按名称模糊查找代码符号。", find_symbol,
         {"type": "object", "properties": {**file_id, "name": {"type": "string"}},
          "required": ["file_id", "name"]}),
        ("find_definition", "定位代码符号定义。", find_definition,
         {"type": "object", "properties": {**file_id, "name": {"type": "string"}},
          "required": ["file_id", "name"]}),
        ("find_references", "查找代码符号的引用关系。", find_references,
         {"type": "object", "properties": {**file_id, "name": {"type": "string"}},
          "required": ["file_id", "name"]}),
        ("get_call_graph", "获取代码符号的调用关系。", get_call_graph,
         {"type": "object", "properties": {**file_id, "symbol": {"type": "string"},
          "direction": {"type": "string", "default": "both"}},
          "required": ["file_id", "symbol"]}),
    ]
    return [
        ToolSpec(name=name, description=description, handler=handler, parameters=params)
        for name, description, handler, params in definitions
    ]
