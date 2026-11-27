"""后端分层边界静态守护测试：防止 domain/ 与 app/ 出现反向依赖。"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
DOMAIN_ROOT = SRC_ROOT / "domain"
APP_ROOT = SRC_ROOT / "app"

# domain 层绝对禁止依赖的内部分层与外部框架/I-O 包。
DOMAIN_FORBIDDEN_ROOTS = frozenset(
    {
        "api",
        "app",
        "adapter",
        "infrastructure",
        "bootstrap",
        "cli",
        "sqlalchemy",
        "fastapi",
        "starlette",
        "redis",
        "httpx",
        "httpx2",
        "asyncpg",
        "minio",
    }
)

# app 层运行时严禁依赖上层入口、具体适配实现、基础设施或组合根。
APP_RUNTIME_FORBIDDEN_ROOTS = frozenset(
    {
        "api",
        "adapter",
        "infrastructure",
        "bootstrap",
        "cli",
    }
)

# app 层在 if TYPE_CHECKING 分支内允许按收敛准则标注仓储类型，但仍禁止依赖入口/组合根/CLI。
APP_TYPE_CHECKING_FORBIDDEN_ROOTS = frozenset(
    {
        "api",
        "bootstrap",
        "cli",
    }
)


@dataclass(frozen=True, slots=True)
class ImportRecord:
    """AST 扫描提取的单条导入记录。"""

    file_path: Path
    lineno: int
    module_name: str
    root_package: str
    in_type_checking: bool


def _is_type_checking_guard(test: ast.expr) -> bool:
    """判断 if 条件是否为 `TYPE_CHECKING` 或 `<typing>.TYPE_CHECKING`。"""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _resolve_from_imports(file_path: Path, node: ast.ImportFrom) -> list[str]:
    """将 ImportFrom（含相对导入与根包级 `from .. import pkg`）解析为完整模块路径列表。"""
    if node.level == 0:
        return [node.module] if node.module else []
    rel_parts = file_path.relative_to(SRC_ROOT).parts
    # 包目录部分（去掉文件名）；__init__.py 自身即代表该包目录。
    package_parts = list(rel_parts[:-1])
    up_levels = node.level - 1
    if up_levels > len(package_parts):
        return [node.module] if node.module else []
    base_parts = package_parts[: len(package_parts) - up_levels]
    if node.module:
        base_parts.extend(node.module.split("."))
        return [".".join(base_parts)]
    # 回到 SRC_ROOT 根层级且 node.module 为空时（如 `from .. import adapter`），
    # 每个导入别名本身即目标顶层包/模块名。
    if not base_parts:
        return [alias.name for alias in node.names if alias.name != "*"]
    return [".".join(base_parts)]


def _extract_dynamic_import_target(node: ast.Call) -> str | None:
    """提取静态字符串字面量形式的 `import_module(...)` / `__import__(...)` 目标模块名。"""
    is_dynamic_import = (
        isinstance(node.func, ast.Name) and node.func.id in {"import_module", "__import__"}
    ) or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
    if not is_dynamic_import:
        return None

    target_expr: ast.expr | None = node.args[0] if node.args else None
    if target_expr is None:
        for kw in node.keywords:
            if kw.arg == "name":
                target_expr = kw.value
                break
    if isinstance(target_expr, ast.Constant) and isinstance(target_expr.value, str):
        module_name = target_expr.value.strip()
        if module_name:
            return module_name
    return None


def _append_record(
    out: list[ImportRecord],
    *,
    file_path: Path,
    lineno: int,
    module_name: str,
    in_type_checking: bool,
) -> None:
    """规范化并追加一条导入记录，跳过前导为空的异常模块名。"""
    root_package = module_name.split(".", 1)[0]
    if not root_package:
        return
    out.append(
        ImportRecord(
            file_path=file_path,
            lineno=lineno,
            module_name=module_name,
            root_package=root_package,
            in_type_checking=in_type_checking,
        )
    )


def _collect_imports(
    node: ast.AST,
    *,
    file_path: Path,
    in_type_checking: bool,
    out: list[ImportRecord],
) -> None:
    """递归遍历 AST 节点，精准区分运行时分支与 if TYPE_CHECKING 分支。"""
    if isinstance(node, ast.If):
        for expr in ast.iter_child_nodes(node.test):
            _collect_imports(expr, file_path=file_path, in_type_checking=in_type_checking, out=out)
        guard_active = in_type_checking or _is_type_checking_guard(node.test)
        for stmt in node.body:
            _collect_imports(stmt, file_path=file_path, in_type_checking=guard_active, out=out)
        for stmt in node.orelse:
            _collect_imports(stmt, file_path=file_path, in_type_checking=in_type_checking, out=out)
        return

    if isinstance(node, ast.Import):
        for alias in node.names:
            _append_record(
                out,
                file_path=file_path,
                lineno=node.lineno,
                module_name=alias.name,
                in_type_checking=in_type_checking,
            )
        return

    if isinstance(node, ast.ImportFrom):
        for resolved in _resolve_from_imports(file_path, node):
            _append_record(
                out,
                file_path=file_path,
                lineno=node.lineno,
                module_name=resolved,
                in_type_checking=in_type_checking,
            )
        return

    if isinstance(node, ast.Call):
        dynamic_target = _extract_dynamic_import_target(node)
        if dynamic_target is not None:
            _append_record(
                out,
                file_path=file_path,
                lineno=node.lineno,
                module_name=dynamic_target,
                in_type_checking=in_type_checking,
            )

    for child in ast.iter_child_nodes(node):
        _collect_imports(child, file_path=file_path, in_type_checking=in_type_checking, out=out)


def _scan_directory_imports(directory: Path) -> list[ImportRecord]:
    """扫描指定目录树下全部 Python 文件的导入语句。"""
    records: list[ImportRecord] = []
    for file_path in sorted(directory.rglob("*.py")):
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        _collect_imports(tree, file_path=file_path, in_type_checking=False, out=records)
    return records


def _format_violations(violations: list[ImportRecord]) -> str:
    """将违规导入格式化为包含相对路径、行号与模块名的清晰报错列表。"""
    repo_root = SRC_ROOT.parents[1]
    lines = [
        f"  - {item.file_path.relative_to(repo_root)}:{item.lineno} -> {item.module_name}"
        for item in violations
    ]
    return "\n".join(lines)


def test_domain_has_no_forbidden_imports() -> None:
    """domain/ 任意导入（含 TYPE_CHECKING）不得依赖上层分层或外部框架/I-O 包。"""
    records = _scan_directory_imports(DOMAIN_ROOT)
    violations = [item for item in records if item.root_package in DOMAIN_FORBIDDEN_ROOTS]
    assert not violations, "domain/ 存在禁止依赖:\n" + _format_violations(violations)


def test_app_has_no_forbidden_runtime_imports() -> None:
    """app/ 运行时导入（排除 TYPE_CHECKING 分支）不得依赖上层或基础设施。"""
    records = _scan_directory_imports(APP_ROOT)
    violations = [
        item
        for item in records
        if not item.in_type_checking and item.root_package in APP_RUNTIME_FORBIDDEN_ROOTS
    ]
    assert not violations, "app/ 运行时存在反向依赖:\n" + _format_violations(violations)


def test_app_type_checking_has_no_forbidden_imports() -> None:
    """app/ 在 if TYPE_CHECKING 分支内不得依赖 api、bootstrap 或 cli。"""
    records = _scan_directory_imports(APP_ROOT)
    violations = [
        item
        for item in records
        if item.in_type_checking and item.root_package in APP_TYPE_CHECKING_FORBIDDEN_ROOTS
    ]
    assert not violations, "app/ TYPE_CHECKING 分支存在禁止依赖:\n" + _format_violations(violations)


def test_ast_scanner_distinguishes_runtime_and_type_checking_imports() -> None:
    """验证 AST 扫描器能准确识别绝对/相对导入、动态导入及 TYPE_CHECKING 分支。"""
    sample = """
import importlib
from importlib import import_module
from typing import TYPE_CHECKING
import bootstrap.worker
from .foo import bar
from ..adapter import repo
from .. import adapter
if TYPE_CHECKING:
    from adapter.run.repository import SqlAlchemyRunRepository
    importlib.import_module("adapter.auth.repository")
else:
    from infrastructure.database import Database
def lazy() -> None:
    import api.errors
    importlib.import_module("infrastructure.feishu")
    import_module(name="bootstrap.worker")
    __import__("adapter.run.repository")
"""
    tree = ast.parse(sample)
    records: list[ImportRecord] = []
    _collect_imports(
        tree,
        file_path=APP_ROOT / "sample.py",
        in_type_checking=False,
        out=records,
    )
    assert [(r.module_name, r.in_type_checking) for r in records] == [
        ("importlib", False),
        ("importlib", False),
        ("typing", False),
        ("bootstrap.worker", False),
        ("app.foo", False),
        ("adapter", False),
        ("adapter", False),
        ("adapter.run.repository", True),
        ("adapter.auth.repository", True),
        ("infrastructure.database", False),
        ("api.errors", False),
        ("infrastructure.feishu", False),
        ("bootstrap.worker", False),
        ("adapter.run.repository", False),
    ]

    # 验证多级子目录中的根层级相对导入（如 app/run/worker.py 的 `from ... import infrastructure`）。
    nested_tree = ast.parse("from ... import infrastructure, api\n")
    nested_records: list[ImportRecord] = []
    _collect_imports(
        nested_tree,
        file_path=APP_ROOT / "run" / "worker.py",
        in_type_checking=False,
        out=nested_records,
    )
    assert [(r.module_name, r.root_package, r.in_type_checking) for r in nested_records] == [
        ("infrastructure", "infrastructure", False),
        ("api", "api", False),
    ]
