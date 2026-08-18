"""Regression tests for the intended module dependency boundaries."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from athena.core.compression.compressor import ContextCompressor
from athena.core.files.runtime import FileIntelligenceRuntime
from athena.core.harness.harness import Harness
from athena.core.memory.memory import MemoryManager
from athena.core.memory.retrieval import HybridRetrievalManager, MemoryRetrievalService
from athena.core.memory.summarizer import ConversationSummarizer
from athena.core.recovery.session_recovery import SessionRecovery
from athena.core.tools.catalog import ToolRegistry
from athena.core.tools.manager import UnifiedToolManager
from athena.core.tools.mcp.adapter import MCPToolAdapter
from athena.core.tools.mcp.manager import MCPManager
from athena.core.tools.providers.agents import build_agent_tool_specs
from athena.gateway.approval import ApprovalManager
from athena.runtime import RuntimeContainer

ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_memory_service_has_no_storage_sdk_dependencies():
    imports = _imports(ROOT / "athena/core/memory/memory.py")
    forbidden = ("sqlalchemy", "chromadb", "athena.db")
    assert not any(module.startswith(forbidden) for module in imports)


def test_sqlite_implementation_does_not_import_file_core_repository():
    sqlite_dir = ROOT / "athena/infrastructure/sqlite"
    imports = set().union(*(_imports(path) for path in sqlite_dir.glob("*.py")))
    assert "athena.core.files.repository" not in imports


def test_database_compatibility_and_migration_hooks_are_absent():
    python_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "athena").rglob("*.py")
    )
    forbidden = (
        "from athena.db",
        "import athena.db",
        "migrate_legacy_builtin_names",
        "SCHEMA_VERSION",
        "_MIGRATIONS",
        "_run_migrations",
        "PRAGMA user_version",
    )
    assert not any(value in python_sources for value in forbidden)


def test_tool_manager_has_no_concrete_tool_name_branches():
    source = (ROOT / "athena/core/tools/manager.py").read_text(encoding="utf-8")
    assert "spawn_sub_agent" not in source
    assert "spawn_parallel_agents" not in source


def test_agent_tool_schema_hides_trusted_runtime_context():
    async def single(task: str, session_id: str, run_id: str) -> str:
        return task

    async def parallel(
        tasks: list[str], session_id: str, run_id: str, max_turns: int
    ) -> str:
        return str(tasks)

    specs = build_agent_tool_specs(single, parallel, "parallel")
    for spec in specs:
        properties = (spec.parameters or {}).get("properties", {})
        assert "session_id" not in properties
        assert "run_id" not in properties
        assert "tool_call_id" not in properties


def test_main_uses_runtime_container_not_service_setters():
    source = (ROOT / "athena/main.py").read_text(encoding="utf-8")
    assert "RuntimeContainer" in source
    for setter in (
        "set_tool_manager", "set_memory_manager", "set_mcp_manager",
        "set_workflow", "set_file_runtime", "set_file_worker",
    ):
        assert setter not in source


def test_runtime_dependencies_are_required():
    required_parameters = {
        RuntimeContainer: tuple(RuntimeContainer.__dataclass_fields__),
        UnifiedToolManager: ("approval_manager",),
        ApprovalManager: ("websocket_manager", "db"),
        MemoryManager: ("settings", "repository", "vector_store"),
        FileIntelligenceRuntime: ("settings", "ws_manager"),
        SessionRecovery: ("ws_manager", "agent_workflow"),
        Harness: ("settings", "db", "ws_manager", "compressor"),
        ContextCompressor: ("settings",),
        HybridRetrievalManager: ("settings",),
        MemoryRetrievalService: ("settings",),
        ConversationSummarizer: ("settings",),
        ToolRegistry: ("catalog",),
        MCPToolAdapter: ("catalog",),
        MCPManager: ("adapter",),
    }
    for dependency, parameter_names in required_parameters.items():
        parameters = inspect.signature(dependency).parameters
        for name in parameter_names:
            assert parameters[name].default is inspect.Parameter.empty


def test_service_locator_compatibility_hooks_are_absent():
    paths = (
        ROOT / "athena/core/llm/provider.py",
        ROOT / "athena/core/memory/memory.py",
        ROOT / "athena/core/tools/manager.py",
        ROOT / "athena/core/tools/mcp/manager.py",
        ROOT / "athena/gateway/approval.py",
        ROOT / "athena/gateway/ws/manager.py",
        ROOT / "athena/gateway/routes/_runtime.py",
        ROOT / "athena/infrastructure/sqlite/database.py",
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    forbidden = (
        "get_llm_provider",
        "set_llm_provider",
        "get_memory_manager",
        "set_memory_manager",
        "get_tool_manager",
        "set_tool_manager",
        "get_mcp_manager",
        "set_mcp_manager",
        "get_approval_manager",
        "set_approval_manager",
        "get_websocket_manager",
        "set_websocket_manager",
        "get_workflow",
        "set_workflow",
        "get_file_runtime",
        "set_file_runtime",
        "get_file_worker",
        "set_file_worker",
        "get_database",
        "close_database",
    )
    assert not any(name in source for name in forbidden)
