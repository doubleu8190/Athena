"""Adapter contracts and normalized extraction types.

定义文件适配器的公共协议和提取结果的数据结构。所有内置适配器
（TextAdapter、PdfAdapter 等）和自定义适配器均需实现 ``FileAdapter``
协议，并返回 ``ExtractionResult`` 作为统一的提取产物。

Typical usage example::

    adapter: FileAdapter = TextAdapter()
    context = ExtractionContext(path=path, workspace=workspace, filename=path.name)
    result = await adapter.extract(context, settings)
    for unit in result.units:
        print(unit.content[:100])
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from athena.config.settings import Settings
from athena.models.file import AdapterInfo


@dataclass
class ExtractionContext:
    """文件提取上下文。

    Attributes:
        path: 文件的绝对路径。
        workspace: 临时工作目录路径，适配器可在此解压或生成中间文件。
        filename: 用户上传时的可见文件名。
        mime_type: 上传或检测得到的 MIME 类型。
    """

    path: Path
    workspace: Path
    filename: str
    mime_type: str = ""


@dataclass
class ExtractedUnit:
    """从文件中提取的最小内容单元。

    每个单元代表一段连续的文本内容，附带定位信息和元数据。
    例如：PDF 的一页、Word 的一个段落、代码文件的完整源码。

    Attributes:
        content: 提取的文本内容。
        locator: 内容在源文件中的定位信息（如页码、段落号、行范围），
            用于前端定位和高亮显示。
        metadata: 内容的语义元数据（如段落样式、OCR 标记、语言标识），
            用于下游分析和过滤。
    """

    content: str
    locator: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractionResult:
    """文件提取的完整结果。

    包含内容单元列表、文件级元数据，以及可选的结构化信息
    （符号表、依赖关系、表格数据）。

    Attributes:
        units: 提取的内容单元列表，按源文件顺序排列。
        metadata: 文件级元数据（如总页数、行数、语言分布）。
        symbols: 代码符号列表，每项含 name/qualified_name/kind/start_line 等字段，
            仅代码类适配器填充。
        dependencies: 代码依赖关系列表，每项含 source/target/kind 字段，
            仅代码类适配器填充。
        tables: 表格数据列表，每项含 headers/rows 或 sheet/rows 等字段，
            仅表格类适配器（Excel、CSV、PDF）填充。
    """

    units: list[ExtractedUnit]
    metadata: dict[str, Any] = field(default_factory=dict)
    symbols: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)


class FileAdapter(Protocol):
    """文件适配器协议。

    定义所有文件适配器必须实现的公共接口。适配器负责将特定格式的文件
    （文本、PDF、Word、Excel、图片、代码、压缩包）解析为统一的
    ``ExtractionResult``。

    Attributes:
        info: 适配器注册信息，包含名称、版本、MIME 类型、扩展名和能力列表。
    """

    info: AdapterInfo

    async def extract(
        self,
        context: ExtractionContext,
        settings: Settings,
    ) -> ExtractionResult:
        """从文件中提取内容、符号和表格数据。

        Args:
            context: 文件路径、临时目录、原始文件名和 MIME 类型。
            settings: 全局配置，包含 chunk 大小、压缩参数等。

        Returns:
            ``ExtractionResult``：包含提取的内容单元、元数据和结构化信息。
        """
        ...

    async def analyze(self, path: Path, task: str) -> dict[str, Any]:
        """对文件执行高级分析（如统计摘要、数据透视）。

        Args:
            path: 文件的绝对路径。
            task: 分析任务的自然语言描述。

        Returns:
            分析结果字典，结构因适配器类型而异。
        """
        ...
