"""Regression tests for the intended module dependency boundaries."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from athena.core.compression.compressor import ContextCompressor
from athena.core.files.runtime import FileIntelligenceRuntime
from athena.core.harness.execution_support import ExecutionSupport
from athena.core.memory.long_term_memory import LongTermMemoryService
from athena.core.memory.retrieval import HybridMemoryRetriever, MemoryRetrievalService
from athena.core.memory.distillation import LongTermMemorySummarizer
from athena.core.tools.catalog import ToolRegistry
from athena.core.tools.manager import UnifiedToolManager
from athena.core.tools.mcp.adapter import MCPToolAdapter
from athena.core.tools.mcp.manager import MCPManager
from athena.gateway.approval import ApprovalManager
from athena.container import RuntimeContainer

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


def test_memory_core_has_no_storage_sdk_dependencies():
    imports = _imports(ROOT / "athena/core/memory/long_term_memory.py")
    forbidden = ("sqlalchemy", "pgvector", "athena.db")
    assert not any(module.startswith(forbidden) for module in imports)


def test_file_runtime_has_no_vector_store_sdk_dependencies():
    imports = _imports(ROOT / "athena/core/files/runtime.py")
    forbidden = ("pgvector", "sqlalchemy", "athena.infrastructure.pgvector")
    assert not any(module.startswith(forbidden) for module in imports)


def test_postgres_implementation_does_not_import_file_core_repository():
    postgres_dir = ROOT / "athena/infrastructure/postgre"
    imports = set().union(*(_imports(path) for path in postgres_dir.rglob("*.py")))
    assert "athena.core.files.repository" not in imports


def test_stable_layers_do_not_depend_on_runtime_orchestration():
    """Contracts flow inward; adapters and core helpers must not import runtime."""

    paths = [
        *(ROOT / "athena/infrastructure/postgre/repositories").rglob("*.py"),
        ROOT / "athena/core/tools/manager.py",
        ROOT / "athena/core/harness/turn_executor.py",
    ]
    forbidden = "athena.runtime.orchestration"
    violations = {
        str(path.relative_to(ROOT)): sorted(
            module for module in _imports(path) if module.startswith(forbidden)
        )
        for path in paths
        if any(module.startswith(forbidden) for module in _imports(path))
    }
    assert not violations, violations


def test_orchestration_runtime_uses_stable_contracts_directly():
    """Runtime implementations must not make the compatibility module canonical."""

    paths = (
        ROOT / "athena/runtime/orchestration/plan_dispatcher.py",
        ROOT / "athena/runtime/orchestration/plan_materializer.py",
        ROOT / "athena/runtime/orchestration/plan_result_synthesizer.py",
        ROOT / "athena/runtime/orchestration/worker.py",
    )
    forbidden = {
        "athena.runtime.orchestration.contracts",
        "athena.runtime.orchestration.policies",
    }
    violations = {
        str(path.relative_to(ROOT)): sorted(_imports(path) & forbidden)
        for path in paths
        if _imports(path) & forbidden
    }
    assert not violations, violations


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


def test_main_uses_runtime_container_not_service_setters():
    source = (ROOT / "athena/main.py").read_text(encoding="utf-8")
    assert "RuntimeContainer" in source
    for setter in (
        "set_tool_manager", "set_memory_manager", "set_mcp_manager",
        "set_workflow", "set_file_runtime",
    ):
        assert setter not in source


def test_runtime_dependencies_are_required():
    required_parameters = {
        RuntimeContainer: tuple(RuntimeContainer.__dataclass_fields__),
        ApprovalManager: ("event_publisher", "agent_store"),
        LongTermMemoryService: ("settings", "repository", "vector_store"),
        FileIntelligenceRuntime: ("settings", "event_publisher"),
        ExecutionSupport: ("settings", "db", "event_publisher"),
        ContextCompressor: ("settings",),
        HybridMemoryRetriever: ("settings",),
        MemoryRetrievalService: ("settings",),
        LongTermMemorySummarizer: ("settings",),
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
        ROOT / "athena/core/memory/long_term_memory.py",
        ROOT / "athena/core/tools/manager.py",
        ROOT / "athena/core/tools/mcp/manager.py",
        ROOT / "athena/gateway/approval.py",
        ROOT / "athena/infrastructure/postgre/database.py",
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
        "get_database",
        "close_database",
    )
    assert not any(name in source for name in forbidden)


def test_provider_test_endpoint_uses_llm_provider() -> None:
    """提供商连通性测试不得绕过统一的 LLMProvider 调用边界。"""
    source = (ROOT / "athena/gateway/routes/providers.py").read_text(encoding="utf-8")

    assert "from athena.core.llm.provider import LLMProvider" in source
    assert "LLMProvider.from_config" in source
    assert "_create_chat_model" not in source
