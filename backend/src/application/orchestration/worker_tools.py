"""Restricted read-only tools available to independent Worker executions."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import hashlib
from typing import Any

from application.tools.service import ToolService
from domain.files import FileStoragePort
from domain.tools import ToolExecutionRequest


WORKER_READ_ONLY_TOOL_NAMES = frozenset(
    {"read_local_file", "read_file", "list_directory", "list_files"}
)


async def list_worker_read_tools(tool_service: Any) -> tuple[str, ...]:
    """Return registered, enabled, low-risk read-only tools for planning."""
    return tuple(
        str(item["name"]) for item in await worker_read_tool_manifest(tool_service)
    )


async def worker_read_tool_manifest(tool_service: Any) -> tuple[dict[str, Any], ...]:
    """Return safe tool schemas without exposing mutating capabilities."""
    list_tools = getattr(tool_service, "list_tools", None)
    if not callable(list_tools):
        return ()
    configured = await list_tools()
    return tuple(
        {
            "name": name,
            "description": str(getattr(tool, "description", "")),
            "parameters": dict(getattr(tool, "parameters", {}) or {}),
        }
        for name, tool in sorted(
            (
                (str(getattr(tool, "name", "")), tool)
                for tool in configured
                if _is_worker_read_tool(tool, str(getattr(tool, "name", "")))
            ),
            key=lambda item: item[0],
        )
    )


class WorkerToolExecutor:
    """Execute only task-authorized, currently enabled read-only tools."""

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
        if config is None or not _is_worker_read_tool(config, name):
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
        result = await self._tool_service.execute(
            ToolExecutionRequest(
                tool_name=name,
                arguments=dict(arguments),
                session_id=session_id,
                run_id=run_id,
                plan_id=str(execution.get("plan_id", "")) or None,
                task_id=task_id,
                tool_call_id=tool_call_id,
                agent_role="worker",
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


def _is_worker_read_tool(config: Any, name: str) -> bool:
    """Workers may use only explicitly configured low-risk read operations."""
    return (
        name in WORKER_READ_ONLY_TOOL_NAMES
        and bool(getattr(config, "enabled", False))
        and not bool(getattr(config, "require_approval", True))
        and str(getattr(config, "risk_level", "")).lower() == "low"
        and str(getattr(config, "execution_mode", "")).lower() == "native"
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
    "WORKER_READ_ONLY_TOOL_NAMES",
    "WorkerToolExecutor",
    "list_worker_read_tools",
    "worker_read_tool_manifest",
]
