"""目标结构的静态依赖边界测试。

测试只扫描 backend/src，不导入旧 athena 包，方便迁移期间独立运行。
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / "src"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _all_imports(directory: str) -> set[str]:
    root = ROOT / directory
    return set().union(*(_imports(path) for path in root.rglob("*.py"))) if root.exists() else set()


def test_domain_does_not_depend_on_outer_layers() -> None:
    imports = _all_imports("domain")
    forbidden_prefixes = (
        "interfaces",
        "application",
        "infrastructure",
        "fastapi",
        "starlette",
        "sqlalchemy",
        "langgraph",
        "pydantic",
        "athena.",
    )
    assert not any(module.startswith(forbidden) for module in imports for forbidden in forbidden_prefixes)


def test_application_does_not_depend_on_web_or_concrete_storage() -> None:
    imports = _all_imports("application")
    forbidden_prefixes = (
        "interfaces",
        "infrastructure.persistence",
        "fastapi",
        "starlette",
        "sqlalchemy",
    )
    assert not any(module.startswith(forbidden) for module in imports for forbidden in forbidden_prefixes)


def test_interfaces_do_not_depend_on_persistence_models() -> None:
    imports = _all_imports("interfaces")
    forbidden_prefixes = (
        "infrastructure.persistence",
        "sqlalchemy",
    )
    assert not any(module.startswith(forbidden) for module in imports for forbidden in forbidden_prefixes)


def test_interfaces_do_not_depend_on_concrete_repositories_or_old_runtime() -> None:
    imports = _all_imports("interfaces")
    forbidden_prefixes = (
        "infrastructure.persistence.postgres.repositories",
        "infrastructure",
        "athena.",
    )
    assert not any(module.startswith(forbidden) for module in imports for forbidden in forbidden_prefixes)


def test_outer_layers_do_not_depend_on_bootstrap_configuration() -> None:
    for directory in ("interfaces", "infrastructure"):
        imports = _all_imports(directory)
        assert not any(
            module == "bootstrap.config" or module.startswith("bootstrap.config.")
            for module in imports
        )


def test_restructured_tree_does_not_import_outer_athena_package() -> None:
    """整个迁移目标包必须能脱离旧 athena 源码被导入。"""
    imports = set().union(*(_imports(path) for path in ROOT.rglob("*.py")))
    assert not any(
        module == "athena" or module.startswith("athena.")
        for module in imports
    )


def test_postgres_bootstrap_only_assembles_dependencies() -> None:
    """生产 bootstrap 不应重新引入嵌套业务用例函数。"""
    path = ROOT / "bootstrap" / "postgres.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    builder = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "build_postgres_dependencies"
    )
    nested_functions = [
        node
        for node in ast.walk(builder)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node is not builder
    ]
    assert not nested_functions


def test_removed_migration_aliases_do_not_return() -> None:
    """生产源码不能重新注册已删除的迁移别名或隐藏路由。"""
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in ROOT.rglob("*.py")
    )
    forbidden = (
        "TargetLLMProvider",
        "TargetEmbeddingProvider",
        "NativeTextParser",
        "line_chunker",
        "NoopVectorIndexer",
        "NoopGraphIndexer",
        "runtime_repositories",
        "PostgresRetrievalQueryRepository",
        "/respond",
        "{file_id}",
    )
    assert not any(value in source for value in forbidden)


def test_boundary_checker_rejects_a_deliberate_forbidden_import(tmp_path: Path) -> None:
    """边界检查逻辑必须能捕获一个明确的反向依赖。"""
    violating_module = tmp_path / "violating.py"
    violating_module.write_text(
        "from infrastructure.persistence import models\n",
        encoding="utf-8",
    )

    imports = _imports(violating_module)
    forbidden_prefixes = ("infrastructure.persistence",)

    assert any(
        module.startswith(forbidden)
        for module in imports
        for forbidden in forbidden_prefixes
    )
