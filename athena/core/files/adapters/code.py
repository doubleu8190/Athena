"""源代码文件适配器和代码索引辅助函数。"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from typing import Any

from athena.config.settings import Settings
from athena.core.files.extraction import (
    ExtractedUnit,
    ExtractionContext,
    ExtractionResult,
)
from athena.models.file import AdapterInfo

from .common import CODE_EXTENSIONS, _decode, logger


class CodeAdapter:
    """源代码文件适配器，支持符号索引和依赖关系提取。"""

    info = AdapterInfo(
        name="code",
        version="2.0",
        mime_types=["text/x-python", "application/javascript", "text/x-java-source"],
        extensions=sorted(CODE_EXTENSIONS),
        capabilities=[
            "read",
            "search",
            "summarize",
            "analyze",
            "symbols",
            "references",
            "call_graph",
        ],
    )

    async def extract(
        self, context: ExtractionContext, settings: Settings
    ) -> ExtractionResult:
        """提取代码文件内容和符号索引。"""
        relative_path = Path(context.filename).name
        return await asyncio.to_thread(self._extract_one, context.path, relative_path)

    def _extract_one(self, path: Path, relative_path: str) -> ExtractionResult:
        """同步提取源码、符号表和依赖关系。"""
        text = _decode(path.read_bytes())
        language = CODE_EXTENSIONS.get(Path(relative_path).suffix.lower(), "text")
        symbols, dependencies = _code_index(text, language, relative_path)
        return ExtractionResult(
            units=[
                ExtractedUnit(
                    text,
                    {"path": relative_path, "start_line": 1},
                    {"language": language, "kind": "source"},
                )
            ],
            metadata={"language": language, "lines": len(text.splitlines())},
            symbols=symbols,
            dependencies=dependencies,
        )

    async def analyze(self, path: Path, task: str) -> dict[str, Any]:
        """分析代码文件并返回语言和符号统计。"""
        result = await asyncio.to_thread(self._extract_one, path, path.name)
        return {
            "languages": {result.metadata.get("language", "text"): 1},
            "symbols": len(result.symbols),
        }


def _code_index(
    text: str, language: str, path: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """构建代码文件的符号索引和依赖关系图。"""
    if language == "python":
        try:
            return _python_index(text, path)
        except SyntaxError as exc:
            logger.warning(
                "python_ast_index_failed", path=path, error=str(exc), fallback="regex"
            )
    if language != "python":
        parsed = _tree_sitter_index(text, language, path)
        if parsed is not None:
            return parsed

    symbols: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    pattern = re.compile(
        r"^\s*(?:class|interface|struct|enum|def|function|func|fn)\s+([A-Za-z_$][\w$]*)",
        re.MULTILINE,
    )
    lines = text.splitlines()
    for match in pattern.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        symbols.append(
            {
                "path": path,
                "language": language,
                "name": match.group(1),
                "qualified_name": match.group(1),
                "kind": "symbol",
                "start_line": line,
                "end_line": line,
                "signature": lines[line - 1][:500],
            }
        )
    for match in re.finditer(
        r"(?:import|from|require\s*\(|#include\s*[<\"])([^\n;\)\">]+)", text
    ):
        dependencies.append(
            {
                "source": path,
                "target": match.group(1).strip(),
                "kind": "import",
                "metadata": {},
            }
        )
    return symbols, dependencies


def _tree_sitter_index(
    text: str, language: str, path: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """使用可选 tree-sitter 语法包构建索引，失败时返回 None。"""
    try:
        from tree_sitter_language_pack import get_parser

        parser = get_parser(language)
        tree = parser.parse(text.encode("utf-8"))
        symbols: list[dict[str, Any]] = []
        dependencies: list[dict[str, Any]] = []
        interesting = {
            "function_definition",
            "function_declaration",
            "method_definition",
            "class_definition",
            "class_declaration",
            "struct_declaration",
            "interface_declaration",
            "enum_declaration",
            "function_item",
        }
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type in interesting:
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    name = text[name_node.start_byte : name_node.end_byte]
                    symbols.append(
                        {
                            "path": path,
                            "language": language,
                            "name": name,
                            "qualified_name": name,
                            "kind": node.type,
                            "start_line": node.start_point[0] + 1,
                            "end_line": node.end_point[0] + 1,
                            "signature": text.splitlines()[node.start_point[0]][:500],
                        }
                    )
            stack.extend(reversed(node.children))
        for match in re.finditer(
            r"(?:import|from|require\s*\(|#include\s*[<\"])([^\n;\)\">]+)", text
        ):
            dependencies.append(
                {
                    "source": path,
                    "target": match.group(1).strip(),
                    "kind": "import",
                    "metadata": {},
                }
            )
        return symbols, dependencies
    except Exception as exc:
        logger.warning(
            "tree_sitter_index_failed",
            path=path,
            language=language,
            error=str(exc),
            fallback="regex",
        )
        return None


def _python_index(
    text: str, path: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """使用 Python AST 构建符号索引和依赖关系图。"""
    tree = ast.parse(text)
    symbols: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    parents: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            qualified = ".".join([*parents, node.name])
            symbols.append(
                {
                    "path": path,
                    "language": "python",
                    "name": node.name,
                    "qualified_name": qualified,
                    "kind": "class",
                    "start_line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                    "signature": f"class {node.name}",
                }
            )
            parents.append(node.name)
            self.generic_visit(node)
            parents.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            qualified = ".".join([*parents, node.name])
            args = ", ".join(arg.arg for arg in node.args.args)
            symbols.append(
                {
                    "path": path,
                    "language": "python",
                    "name": node.name,
                    "qualified_name": qualified,
                    "kind": "function",
                    "start_line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                    "signature": f"def {node.name}({args})",
                }
            )
            parents.append(node.name)
            self.generic_visit(node)
            parents.pop()

        def visit_Import(self, node: ast.Import) -> None:
            for name in node.names:
                dependencies.append(
                    {
                        "source": path,
                        "target": name.name,
                        "kind": "import",
                        "metadata": {"line": node.lineno},
                    }
                )

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            dependencies.append(
                {
                    "source": path,
                    "target": node.module or "",
                    "kind": "import",
                    "metadata": {"line": node.lineno},
                }
            )

        def visit_Call(self, node: ast.Call) -> None:
            target = ""
            if isinstance(node.func, ast.Name):
                target = node.func.id
            elif isinstance(node.func, ast.Attribute):
                target = node.func.attr
            if target:
                dependencies.append(
                    {
                        "source": ".".join(parents) or path,
                        "target": target,
                        "kind": "call",
                        "metadata": {"line": node.lineno},
                    }
                )
            self.generic_visit(node)

    Visitor().visit(tree)
    return symbols, dependencies
