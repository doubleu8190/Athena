"""内置文件适配器统一导出入口。"""

from .code import CodeAdapter
from .excel import ExcelAdapter
from .image import ImageAdapter
from .pdf import PdfAdapter
from .text import TextAdapter
from .word import WordAdapter

__all__ = [
    "CodeAdapter",
    "ExcelAdapter",
    "ImageAdapter",
    "PdfAdapter",
    "TextAdapter",
    "WordAdapter",
]
