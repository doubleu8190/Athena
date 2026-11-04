"""Trace advisory 与 Worker dispatcher 领域行锁的静态顺序回归。"""

from __future__ import annotations

import ast
import inspect
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from functools import lru_cache
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from domain.orchestration.worker_entities import WorkerDispatchLease
from infrastructure.persistence.postgres.repositories import (
    worker_orchestration_repository as repository_module,
)
from infrastructure.persistence.postgres.repositories.worker_orchestration_repository import (
    PostgresWorkerOrchestrationRepository,
)

_COMMAND_ONLY_WRITE_METHODS = (
    "claim_dispatch_command",
    "record_cancel_receipt",
    "freeze_dispatch_body",
    "record_dispatch_receipt",
    "exhaust_dispatch_retries",
    "await_dispatch_result",
)
_PARENT_VALIDATED_WRITE_METHODS = (
    "start_dispatch",
    "prepare_cancel_dispatch",
    "prepare_resume_dispatch",
    "prepare_first_resume_send",
    "record_resume_receipt",
    "prepare_dispatch_send",
    "prepare_first_dispatch_send",
    "retry_dispatch_command",
    "fail_dispatch_command",
    "submit_approval_request",
    "_submit_plan",
    "expire_queued_execution",
    "_record_result",
    "cancel_task",
    "confirm_stopped",
    "record_result",
    "submit_plan",
    "submit_root_plan",
    "timeout_execution",
)
_COMMAND_CREATION_PARENT_LOCKED_METHODS = frozenset({"_submit_plan"})
_DELEGATING_PARENT_VALIDATED_METHODS = {
    "record_result": "_record_result",
    "submit_plan": "_submit_plan",
    "submit_root_plan": "_submit_plan",
    "timeout_execution": "_record_result",
}
_EXPECTED_COMMAND_WRITE_METHOD_COUNT = 25
_SESSION_WRITE_CALLS = frozenset(
    {
        "add",
        "add_all",
        "bulk_save_objects",
        "bulk_update_mappings",
        "delete",
        "execute",
        "merge",
    }
)
_COMMAND_ONLY_FORBIDDEN_REFERENCES = frozenset(
    {
        "_add_worker_execution_span",
        "_finish_worker_execution_span",
        "_lock_worker_write_scope",
        "AgentRunModel",
        "OrchestratorExecutionModel",
        "OrchestratorPlanModel",
        "OrchestratorTaskModel",
        "OrchestrationOutboxModel",
        "RunProgressModel",
        "TraceSpanModel",
        "WorkerApprovalRequestModel",
        "WorkerResultReceiptModel",
        "lock_progress_session",
        "lock_trace_span_scope",
        "text",
    }
)
_COMMAND_ONLY_ALLOWED_MODELS = frozenset(
    {
        "OrchestratorWorkerCommandModel",
        "WorkerCancelReceiptModel",
        "WorkerCommandReceiptModel",
    }
)
_COMMAND_ONLY_FORBIDDEN_SQL = (
    "agent_plans",
    "agent_runs",
    "agent_tasks",
    "run_progress_entries",
    "task_executions",
    "trace_spans",
    "worker_approval_requests",
)


@lru_cache(maxsize=1)
def _repository_tree() -> ast.Module:
    """解析真实仓储源码，供全部静态契约测试共享。"""
    return ast.parse(inspect.getsource(repository_module))


def _repository_class_node(tree: ast.Module) -> ast.ClassDef:
    """返回 Worker orchestration 仓储类节点。"""
    for node in tree.body:
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "PostgresWorkerOrchestrationRepository"
        ):
            return node
    raise AssertionError("worker_orchestration_repository_class_not_found")


def _class_function_map(
    tree: ast.Module,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """收集仓储类中的同步和异步方法，供 helper 递归解析。"""
    return {
        item.name: item
        for item in _repository_class_node(tree).body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _module_function_map(
    tree: ast.Module,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """收集模块级 helper。"""
    return {
        item.name: item
        for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _repository_methods() -> dict[str, ast.AsyncFunctionDef]:
    """返回 Worker orchestration 仓储类中的异步方法。"""
    return {
        name: function
        for name, function in _class_function_map(_repository_tree()).items()
        if isinstance(function, ast.AsyncFunctionDef)
    }


def _session_write_calls(node: ast.AST) -> set[str]:
    """收集方法体中的 AsyncSession 写操作。"""
    return {
        item.func.attr
        for item in ast.walk(node)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and isinstance(item.func.value, ast.Name)
        and item.func.value.id == "session"
        and item.func.attr in _SESSION_WRITE_CALLS
    }


def _mutates_command_object(node: ast.AST) -> bool:
    """识别未加锁时直接修改 command 对象或调用其 lease helper 的写路径。"""
    for item in ast.walk(node):
        targets: list[ast.AST] = []
        if isinstance(item, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if isinstance(item, ast.Assign):
                targets.extend(item.targets)
            else:
                targets.append(item.target)
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "command"
            ):
                return True
        if (
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and item.func.attr == "_clear_command_lease"
        ):
            return True
    return False


def _directly_writes_command(node: ast.AST) -> bool:
    """判断单个函数体是否直接写入 Worker command。"""
    references = _referenced_names(node)
    sql_literals = {
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }
    mentions_command = (
        "OrchestratorWorkerCommandModel" in references
        or any(
            "orchestrator_worker_commands" in sql_literal
            for sql_literal in sql_literals
        )
    )
    has_row_lock = any(
        isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == "with_for_update"
        for item in ast.walk(node)
    )
    if _mutates_command_object(node):
        return True
    return mentions_command and (
        has_row_lock
        or bool(_session_write_calls(node))
    )


def _local_callees(
    node: ast.AST,
    *,
    class_name: str,
) -> set[tuple[str, str]]:
    """解析可静态确定的本地 helper 调用。

    支持 `self.helper(...)`、`cls.helper(...)`、`ClassName.helper(...)` 和
    模块级 `helper(...)`。动态 `getattr` 或函数变量调用不在此静态契约内。
    """
    callees: set[tuple[str, str]] = set()
    for item in ast.walk(node):
        if not isinstance(item, ast.Call):
            continue
        function = item.func
        if isinstance(function, ast.Name):
            callees.add(("module", function.id))
        elif (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id in {"self", "cls", class_name}
        ):
            callees.add(("class", function.attr))
    return callees


def _reaches_command_write(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    class_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    module_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    class_name: str,
    visited: set[int] | None = None,
) -> bool:
    """递归判断函数是否直接或经本地 helper 间接写入 command。"""
    seen = visited if visited is not None else set()
    node_id = id(node)
    if node_id in seen:
        return False
    seen.add(node_id)
    if _directly_writes_command(node):
        return True
    functions = {
        "class": class_functions,
        "module": module_functions,
    }
    for scope, name in _local_callees(node, class_name=class_name):
        helper = functions[scope].get(name)
        if helper is not None and _reaches_command_write(
            helper,
            class_functions=class_functions,
            module_functions=module_functions,
            class_name=class_name,
            visited=seen,
        ):
            return True
    return False


def _command_write_method_names(tree: ast.Module | None = None) -> set[str]:
    """递归枚举可能直接或经本地 helper 修改 Worker command 的事务方法。

    入口限定为仓储类的异步方法；同步或模块级函数作为 helper 参与递归分析，
    但不单独分类。visited 集合按 AST 节点去重，可终止 helper 互相调用和自递归。
    """
    source = _repository_tree() if tree is None else tree
    class_node = _repository_class_node(source)
    class_functions = _class_function_map(source)
    module_functions = _module_function_map(source)
    return {
        method_name
        for method_name, method in class_functions.items()
        if isinstance(method, ast.AsyncFunctionDef)
        and _reaches_command_write(
            method,
            class_functions=class_functions,
            module_functions=module_functions,
            class_name=class_node.name,
        )
    }


class _Transaction(AbstractAsyncContextManager[None]):
    """为空事务入口提供异步上下文。"""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class _Session:
    """记录 Trace advisory 与第一条领域行锁查询的先后顺序。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._gets = iter(
            (
                SimpleNamespace(task_id="task-1"),
                SimpleNamespace(task_id="task-1", plan_id="plan-1"),
                SimpleNamespace(run_id="run-1"),
                SimpleNamespace(run_id="run-1", session_id="session-1"),
            )
        )

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def begin(self) -> AbstractAsyncContextManager[None]:
        return _Transaction()

    async def get(self, *_args: object, **_kwargs: object) -> Any:
        return next(self._gets)

    async def scalar(self, statement: object, *_args: object, **_kwargs: object) -> None:
        if getattr(statement, "_for_update_arg", None) is not None:
            self.calls.append("row_lock")
        return None


def _method_node(method_name: str) -> ast.AsyncFunctionDef:
    """从真实仓储源码中提取待审计方法节点。"""
    method = _repository_methods().get(method_name)
    if method is None:
        raise AssertionError(f"repository_method_not_found:{method_name}")
    return method


def _referenced_names(node: ast.AST) -> set[str]:
    """收集方法体引用的变量、枚举名和属性名。"""
    names: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Name):
            names.add(item.id)
        elif isinstance(item, ast.Attribute):
            names.add(item.attr)
    return names


def test_every_command_write_method_has_an_explicit_lock_profile() -> None:
    """任何新增 command 写方法都必须明确分类，禁止静默绕过锁序契约。"""
    discovered = _command_write_method_names()
    classified = set(_COMMAND_ONLY_WRITE_METHODS) | set(
        _PARENT_VALIDATED_WRITE_METHODS
    )
    assert len(discovered) == _EXPECTED_COMMAND_WRITE_METHOD_COUNT
    assert discovered == classified, (
        "worker command write methods must be explicitly classified; "
        f"unclassified={sorted(discovered - classified)}; "
        f"stale={sorted(classified - discovered)}"
    )


def test_unclassified_method_calling_command_write_helper_is_detected() -> None:
    """入口方法只调用本地递归 helper 时，也必须被扫描并判为未分类。"""
    tree = ast.parse(
        """
async def _write_command_helper(command):
    await _finish_command_helper(command)


async def _finish_command_helper(command):
    command.status = "accepted"
    await _write_command_helper(command)


class PostgresWorkerOrchestrationRepository:
    async def uncategorized_command_writer(self):
        await _write_command_helper(None)
"""
    )

    discovered = _command_write_method_names(tree)
    classified = set(_COMMAND_ONLY_WRITE_METHODS) | set(
        _PARENT_VALIDATED_WRITE_METHODS
    )

    assert discovered == {"uncategorized_command_writer"}
    assert discovered - classified == {"uncategorized_command_writer"}


@pytest.mark.parametrize("method_name", _COMMAND_ONLY_WRITE_METHODS)
def test_command_only_dispatch_writes_keep_transaction_boundary(
    method_name: str,
) -> None:
    """command-only 写事务不得扩展为父领域、Trace 或 Progress 写入。

    如果未来确实需要父级状态，必须先移除本方法的 command-only 标记，再解析
    Run/Session 身份并走 `_lock_worker_write_scope`，从而保持统一锁序。
    """
    method = _method_node(method_name)
    assert "这是 command-only 事务" in (ast.get_docstring(method) or "")
    references = _referenced_names(method)
    assert not (references & _COMMAND_ONLY_FORBIDDEN_REFERENCES)
    assert {
        name for name in references if name.endswith("Model")
    } <= _COMMAND_ONLY_ALLOWED_MODELS
    session_writes = {
        item.func.attr
        for item in ast.walk(method)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and isinstance(item.func.value, ast.Name)
        and item.func.value.id == "session"
        and item.func.attr in {"add", "execute", "get"}
    }
    assert not session_writes
    sql_literals = {
        item.value
        for item in ast.walk(method)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }
    assert not any(
        table_name in sql_literal
        for sql_literal in sql_literals
        for table_name in _COMMAND_ONLY_FORBIDDEN_SQL
    )


@pytest.mark.parametrize("method_name", _PARENT_VALIDATED_WRITE_METHODS)
def test_parent_validated_dispatch_write_locks_scope_before_domain_rows(
    method_name: str,
) -> None:
    """校验父级的写方法必须保留统一 advisory 前缀。"""
    method = _method_node(method_name)
    scope_calls = [
        item
        for item in ast.walk(method)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id == "_lock_worker_write_scope"
    ]
    domain_row_locks = [
        item
        for item in ast.walk(method)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == "with_for_update"
    ]
    assert "这是 command-only 事务" not in (ast.get_docstring(method) or "")
    delegated_target = _DELEGATING_PARENT_VALIDATED_METHODS.get(method_name)
    if delegated_target is not None:
        # 公开包装入口不直接访问数据库；它只能委托给已显式分类的父级 helper。
        assert not _directly_writes_command(method)
        assert not scope_calls
        assert not domain_row_locks
        assert ("class", delegated_target) in _local_callees(
            method,
            class_name="PostgresWorkerOrchestrationRepository",
        )
        assert delegated_target in _PARENT_VALIDATED_WRITE_METHODS
        return
    if method_name in _COMMAND_CREATION_PARENT_LOCKED_METHODS:
        # 新 Plan/Execution/Command 创建没有既有 Execution 行锁，禁止误加
        # advisory；必须在 Session/Run 行锁之后才插入 command。
        command_add_lines = [
            item.lineno
            for item in ast.walk(method)
            if isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and isinstance(item.func.value, ast.Name)
            and item.func.value.id == "session"
            and item.func.attr == "add"
            and any(
                isinstance(child, ast.Name)
                and child.id == "OrchestratorWorkerCommandModel"
                for argument in item.args
                for child in ast.walk(argument)
            )
        ]
        observability_references = _referenced_names(method) & {
            "RunProgressModel",
            "TraceSpanModel",
            "_add_worker_execution_span",
            "_finish_worker_execution_span",
            "lock_progress_session",
            "lock_trace_span_scope",
        }
        assert not scope_calls
        assert domain_row_locks
        assert command_add_lines
        assert "AgentRunModel" in _referenced_names(method)
        assert min(item.lineno for item in domain_row_locks) < min(
            command_add_lines
        )
        assert not observability_references
        return
    assert scope_calls
    assert domain_row_locks
    assert min(item.lineno for item in scope_calls) < min(
        item.lineno for item in domain_row_locks
    )


@pytest.mark.asyncio
async def test_start_dispatch_locks_trace_scope_before_domain_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dispatcher 必须先取得 Run Trace advisory，再执行任何 FOR UPDATE。"""
    session = _Session()

    async def fake_trace_lock(current: _Session, run_id: str) -> None:
        assert current is session
        assert run_id == "run-1"
        current.calls.append("trace_lock")

    async def fake_progress_lock(current: _Session, session_id: str) -> None:
        assert current is session
        assert session_id == "session-1"
        current.calls.append("progress_lock")

    monkeypatch.setattr(
        repository_module,
        "lock_trace_span_scope",
        fake_trace_lock,
    )
    monkeypatch.setattr(
        repository_module,
        "lock_progress_session",
        fake_progress_lock,
    )
    repository = PostgresWorkerOrchestrationRepository(
        lambda: session,
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    lease = WorkerDispatchLease(
        command_id=str(uuid4()),
        execution_id=str(uuid4()),
        lease_owner="dispatcher",
        lease_token=str(uuid4()),
        attempt=0,
        command={},
    )

    result = await repository.start_dispatch(
        lease,
        lease_seconds=15,
        assignment_retry_seconds=0.1,
    )

    assert result is None
    assert session.calls[:2] == ["trace_lock", "progress_lock"]
    assert session.calls[2:] == ["row_lock"] * (len(session.calls) - 2)
