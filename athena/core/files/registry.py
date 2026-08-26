"""基于配置的适配器注册表。"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from athena.core.files.adapters import (
    CodeAdapter, ExcelAdapter, ImageAdapter, PdfAdapter, TextAdapter, WordAdapter,
)
from athena.core.files.base import FileAdapter


class AdapterRegistry:
    """表示 AdapterRegistry 组件，封装相关状态和行为。
    """
    def __init__(self) -> None:
        """初始化当前对象。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        adapters: list[FileAdapter] = [
            PdfAdapter(), WordAdapter(), ExcelAdapter(), ImageAdapter(), CodeAdapter(), TextAdapter(),
        ]
        self._adapters = {adapter.info.name: adapter for adapter in adapters}

    def list(self) -> list[FileAdapter]:
        """列出数据。

        返回值：
            list[FileAdapter]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return list(self._adapters.values())

    def supported_extensions(self) -> list[str]:
        """返回启用适配器支持的所有可选扩展名。"""
        return sorted({extension for adapter in self._adapters.values() for extension in adapter.info.extensions})

    def select(self, filename: str, mime_type: str = "") -> FileAdapter:
        """选择适配器或资源。

        参数：
            filename (str): 原始文件名。
            mime_type (str): 文件 MIME 类型。

        返回值：
            FileAdapter: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        lower = filename.lower()
        extension = Path(lower).suffix
        if extension in (".doc", ".xls"):
            target = "DOCX" if extension == ".doc" else "XLSX"
            raise ValueError(f"不支持旧版 {extension[1:].upper()} 格式，请先转换为 {target} 后重新上传")
        for adapter in self._adapters.values():
            if extension in adapter.info.extensions:
                mime_adapter = next(
                    (item for item in self._adapters.values() if mime_type in item.info.mime_types),
                    None,
                )
                if mime_adapter is not None and mime_adapter.info.name != adapter.info.name:
                    raise ValueError(f"文件扩展名与 MIME 类型不匹配: {filename} / {mime_type}")
                return adapter
        for adapter in self._adapters.values():
            if mime_type in adapter.info.mime_types:
                return adapter
        guessed = mimetypes.guess_type(filename)[0] or ""
        for adapter in self._adapters.values():
            if guessed in adapter.info.mime_types:
                return adapter
        raise ValueError(f"不支持的文件类型: {filename}")
