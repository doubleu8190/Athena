"""Keep application code independent from infrastructure implementations."""

from __future__ import annotations

import ast
from pathlib import Path


APPLICATION_SOURCE = Path(__file__).resolve().parents[2] / "src" / "application"


def test_application_modules_do_not_import_infrastructure() -> None:
    violations: list[str] = []
    for path in APPLICATION_SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = (alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = (node.module,)
            else:
                continue
            if any(
                module == "infrastructure" or module.startswith("infrastructure.")
                for module in modules
            ):
                violations.append(str(path.relative_to(APPLICATION_SOURCE)))

    assert violations == []
