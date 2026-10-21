"""Governed tools available to independent Worker executions."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import hashlib
import json
from typing import Any

from application.tools.service import ToolService
from domain.files import FileStoragePort
from domain.tools import ToolExecutionRequest


async def list_worker_tools(tool_service: Any) -> tuple[str, ...]:
    """Return registered native tools available to Worker planning."""
    return tuple(
        str(item["name"]) for item in await worker_tool_manifest(tool_service)
    )


async def worker_tool_manifest(tool_service: Any) -> tuple[dict[str, Any], ...]:
    """Return enabled native tools with their deterministic approval policy."""
    list_tools = getattr(tool_service, "list_tools", None)
    if not callable(list_tools):
        return ()
    configured = await list_tools()
    return tuple(
        {
            "name": name,
            "description": str(getattr(tool, "description", "")),
            "parameters": dict(getattr(tool, "parameters", {}) or {}),
            "risk_level": _risk_level(tool),
            "requires_approval": _requires_approval(tool),
        }
        for name, tool in sorted(
            (
                (str(getattr(tool, "name", "")), tool)
                for tool in configured
                if _is_worker_tool(tool)
            ),
            key=lambda item: item[0],
        )
    )


class WorkerToolExecutor:
    """Execute task-authorized native tools through deterministic governance."""

    def __init__(
        self,
        tool_service: ToolService,
        *,
        storage: FileStoragePort | None = None,
        inline_result_bytes: int = 8192,
        max_result_read_chars: int = 16000,
    ) -> None:
        if inline_result_bytes < 1 or max_result_read_chars < 1:
            raise ValueError("worker result limits must be positive")
        self._tool_service = tool_service
        self._storage = storage
        self._inline_result_bytes = inline_result_bytes
        self._max_result_read_chars = max_result_read_chars

    async def __call__(
        self,
        *,
        tool_call: Any,
        execution: Mapping[str, Any],
        tool_round: int,
        call_index: int,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Revalidate capability and governance immediately before execution."""
        if not isinstance(tool_call, Mapping):
            return _denied("invalid_tool_call")
        name = str(tool_call.get("name", "")).strip()
        if name == "read_worker_result":
            return await self._read_worker_result(tool_call, execution)
        task = execution.get("task", {})
        task = task if isinstance(task, Mapping) else {}
        allowed_tools = {
            str(value) for value in task.get("allowed_tools", ())
        }
        if not name or name not in allowed_tools:
            return _denied("tool_not_allowed_for_task", name=name)

        config = await self._tool_service.get(name)
        if config is None or not _is_worker_tool(config):
            return _denied("tool_not_available_to_worker", name=name)

        arguments = tool_call.get("arguments", tool_call.get("args", {}))
        if not isinstance(arguments, Mapping):
            return _denied("invalid_tool_arguments", name=name)
        run_id = str(execution.get("run_id", ""))
        session_id = str(execution.get("session_id", ""))
        task_id = str(task.get("task_id", ""))
        generation = int(execution.get("execution_generation", 0))
        if not run_id or not session_id or not task_id or generation < 1:
            return _denied("worker_execution_identity_missing", name=name)

        tool_call_id = (
            f"worker:{task_id}:{generation}:{tool_round}:{call_index}"
        )
        approval_decision = str(tool_call.get("approval_decision", "")).lower()
        if approval_decision == "approved":
            expected_args_hash = hashlib.sha256(
                _strict_json(dict(arguments)).encode("utf-8")
            ).hexdigest()
            expected_operation_key = _operation_key(
                run_id, task_id, generation, tool_round, call_index
            )
            if (
                tool_call.get("args_hash") != expected_args_hash
                or tool_call.get("operation_key") != expected_operation_key
                or tool_call.get("tool_call_id")
                != f"worker:{task_id}:{generation}:{tool_round}:{call_index}"
            ):
                return _denied("worker_approval_operation_mismatch", name=name)
        if _requires_approval(config) and approval_decision != "approved":
            execution_id = str(execution.get("execution_id", ""))
            worker_thread_id = str(
                execution.get("worker_thread_id")
                or f"task:{task_id}:exec:{generation}"
            )
            if not execution_id:
                return _denied("worker_execution_identity_missing", name=name)
            arguments_json = _strict_json(dict(arguments))
            args_hash = hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()
            operation_key = _operation_key(
                run_id, task_id, generation, tool_round, call_index
            )
            approval_id = "approval:" + hashlib.sha256(
                f"{worker_thread_id}:{operation_key}".encode("utf-8")
            ).hexdigest()[:32]
            return {
                "status": "approval_required",
                "approval_request": {
                    "approval_id": approval_id,
                    "session_id": session_id,
                    "run_id": run_id,
                    "plan_id": str(execution.get("plan_id", "")),
                    "task_id": task_id,
                    "execution_id": execution_id,
                    "worker_thread_id": worker_thread_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": name,
                    "arguments": dict(arguments),
                    "risk_level": _risk_level(config),
                    "args_hash": args_hash,
                    "operation_key": operation_key,
                    "tool_round": tool_round,
                    "call_index": call_index,
                },
            }

        result = await self._tool_service.execute(
            ToolExecutionRequest(
                tool_name=name,
                arguments=dict(arguments),
                session_id=session_id,
                run_id=run_id,
                approval_decision=approval_decision or None,
                plan_id=str(execution.get("plan_id", "")) or None,
                task_id=task_id,
                tool_call_id=tool_call_id,
                agent_role="worker",
                operation_key=(
                    str(tool_call.get("operation_key"))
                    if tool_call.get("operation_key")
                    else None
                ),
            )
        )
        payload = {
            "id": tool_call_id,
            "name": name,
            "status": result.status,
            "output": result.output,
            "error": result.error,
            "duration_ms": result.duration_ms,
        }
        if (
            self._storage is not None
            and isinstance(result.output, str)
            and len(result.output.encode("utf-8")) > self._inline_result_bytes
        ):
            blob = await self._storage.save_stream(
                _byte_chunks(result.output.encode("utf-8"))
            )
            identity = (
                f"{run_id}:{task_id}:{generation}:{tool_round}:{call_index}:{blob.sha256}"
            )
            result_ref = "worker-result:" + hashlib.sha256(
                identity.encode("utf-8")
            ).hexdigest()[:32]
            payload["result_ref"] = result_ref
            payload["output"] = result.output[: self._inline_result_bytes]
            payload["truncated"] = True
            payload["_storage_key"] = blob.storage_key
        return payload

    async def _read_worker_result(
        self, tool_call: Mapping[str, Any], execution: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Read a bounded slice only from a result created in this execution."""
        if self._storage is None:
            return _denied("worker_result_storage_unavailable")
        arguments = tool_call.get("arguments", tool_call.get("args", {}))
        if not isinstance(arguments, Mapping):
            return _denied("invalid_tool_arguments", name="read_worker_result")
        result_ref = str(arguments.get("result_ref", ""))
        references = execution.get("_worker_result_refs", {})
        storage_key = references.get(result_ref) if isinstance(references, Mapping) else None
        if not result_ref or not isinstance(storage_key, str):
            return _denied("worker_result_reference_not_found", name="read_worker_result")
        try:
            offset = max(0, int(arguments.get("offset", 0)))
            limit = min(
                self._max_result_read_chars,
                max(1, int(arguments.get("max_chars", self._max_result_read_chars))),
            )
        except (TypeError, ValueError):
            return _denied("invalid_tool_arguments", name="read_worker_result")
        try:
            content = (await self._storage.read(storage_key)).decode(
                "utf-8", errors="replace"
            )
        except FileNotFoundError:
            return _denied("worker_result_reference_expired", name="read_worker_result")
        end = min(len(content), offset + limit)
        return {
            "name": "read_worker_result",
            "result_ref": result_ref,
            "status": "success",
            "output": content[offset:end],
            "offset": offset,
            "next_offset": end,
            "has_more": end < len(content),
        }


def _is_worker_tool(config: Any) -> bool:
    """Workers may use enabled native tools with a known risk classification."""
    risk_level = _risk_level(config)
    return (
        bool(getattr(config, "enabled", False))
        and risk_level in {"low", "medium", "high"}
        and str(getattr(config, "execution_mode", "")).lower() == "native"
    )


def _risk_level(config: Any) -> str:
    """Normalize an enum or plain string risk value."""
    value = getattr(config, "risk_level", "")
    return str(getattr(value, "value", value)).lower()


def _requires_approval(config: Any) -> bool:
    """Require approval when configured or when risk is medium/high."""
    return bool(getattr(config, "require_approval", True)) or _risk_level(
        config
    ) in {"medium", "high"}


def _operation_key(
    run_id: str,
    task_id: str,
    generation: int,
    tool_round: int,
    call_index: int,
) -> str:
    """Build a stable side-effect key within the database column limit."""
    value = f"athena:{run_id}:{task_id}:{generation}:{tool_round}:{call_index}"
    if len(value) <= 128:
        return value
    return "athena:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _strict_json(value: Any) -> str:
    """Serialize an operation deterministically for approval binding."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _denied(code: str, *, name: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "denied", "error": {"code": code}}
    if name:
        result["name"] = name
    return result


async def _byte_chunks(value: bytes) -> AsyncIterator[bytes]:
    """Adapt one result payload to the existing asynchronous storage port."""
    yield value


__all__ = [
    "WorkerToolExecutor",
    "list_worker_tools",
    "worker_tool_manifest",
]
