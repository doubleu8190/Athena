"""文件智能工具声明。"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from athena.core.tools.spec import ToolSpec, get_tool_context
from athena.models.file import FileTaskType

if TYPE_CHECKING:
    from athena.core.files.runtime import FileIntelligenceRuntime


def build_file_tool_specs(runtime: FileIntelligenceRuntime) -> list[ToolSpec]:
    """执行“build file tool specs”操作。

    参数：
        runtime (FileIntelligenceRuntime): 输入参数；其类型和取值约束由方法签名及实现定义。

    返回值：
        list[ToolSpec]: 操作结果；具体语义由调用场景决定。

    异常：
        Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
    """
    def session_id() -> str:
        """执行“session id”操作。

        返回值：
            str: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return get_tool_context().session_id

    async def list_files() -> Any:
        """执行“list files”操作。

        返回值：
            Any: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return await runtime.list_files(session_id())

    async def get_file_info(file_id: str) -> Any:
        """执行“get file info”操作。

        参数：
            file_id (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            Any: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return await runtime.get_file_info(session_id(), file_id)

    async def read_file(
        file_id: str, locator: dict[str, Any] | None = None, limit: int = 10
    ) -> Any:
        """执行“read file”操作。

        参数：
            file_id (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            locator (dict[str, Any] | None): 源文档定位信息。
            limit (int): 最大返回数量；应为非负整数。

        返回值：
            Any: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return await runtime.read_file(session_id(), file_id, locator, limit)

    async def search_file(file_id: str, query: str, limit: int = 10) -> Any:
        """执行“search file”操作。

        参数：
            file_id (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            query (str): 检索或搜索文本；应为非空字符串。
            limit (int): 最大返回数量；应为非负整数。

        返回值：
            Any: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return await runtime.search_file(session_id(), file_id, query, limit)

    async def extract_table(file_id: str) -> Any:
        """执行“extract table”操作。

        参数：
            file_id (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            Any: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return await runtime.extract_table(session_id(), file_id)

    async def summarize_file(file_id: str, summary_type: str = "general") -> Any:
        """执行“summarize file”操作。

        参数：
            file_id (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            summary_type (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            Any: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if runtime.task_queue_available:
            task = await runtime.enqueue_task(
                session_id(), file_id, FileTaskType.FILE_SUMMARY,
                payload={"summary_type": summary_type},
            )
            return {"queued": True, "task": task.model_dump(mode="json")}
        return await runtime.summarize_file(session_id(), file_id, summary_type)

    async def analyze_file(file_id: str, task: str) -> Any:
        """执行“analyze file”操作。

        参数：
            file_id (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            task (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            Any: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return await runtime.analyze_file(session_id(), file_id, task)

    async def analyze_codebase(file_id: str) -> Any:
        """执行“analyze codebase”操作。

        参数：
            file_id (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            Any: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if runtime.task_queue_available:
            task = await runtime.enqueue_task(
                session_id(), file_id, FileTaskType.CODE_ANALYSIS,
            )
            return {"queued": True, "task": task.model_dump(mode="json")}
        return await runtime.analyze_codebase(session_id(), file_id)

    async def find_symbol(file_id: str, name: str) -> Any:
        """执行“find symbol”操作。

        参数：
            file_id (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            name (str): 资源名称或稳定标识。

        返回值：
            Any: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return await runtime.find_symbol(session_id(), file_id, name)

    async def find_definition(file_id: str, name: str) -> Any:
        """执行“find definition”操作。

        参数：
            file_id (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            name (str): 资源名称或稳定标识。

        返回值：
            Any: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return await runtime.find_symbol(session_id(), file_id, name)

    async def find_references(file_id: str, name: str) -> Any:
        """执行“find references”操作。

        参数：
            file_id (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            name (str): 资源名称或稳定标识。

        返回值：
            Any: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return await runtime.get_call_graph(session_id(), file_id, name, "incoming")

    async def get_call_graph(
        file_id: str, symbol: str, direction: str = "both"
    ) -> Any:
        """执行“get call graph”操作。

        参数：
            file_id (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            symbol (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            direction (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            Any: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
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
