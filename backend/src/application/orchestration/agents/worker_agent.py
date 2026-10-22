"""配置化 Worker Agent：执行单个已持久化任务。"""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from application.orchestration.agents.llm_activity import (
    invoke_llm_with_activity,
)
from application.orchestration.worker_tools import worker_tool_manifest
from application.tools.service import ToolService

from domain.events.ports import EventPort
from domain.integrations import LLMPort
from shared.logging import get_logger

logger = get_logger(__name__)


class ConfiguredWorkerAgent:
    """Run one persisted task with the configured model."""

    def __init__(
        self,
        llm: LLMPort,
        event_publisher: EventPort | None = None,
        tool_service: ToolService | None = None,
    ) -> None:
        self._llm = llm
        self._events = event_publisher
        self._tool_service = tool_service

    async def __call__(
        self, *, execution: Mapping[str, Any], **_kwargs: Any
    ) -> dict[str, Any]:
        task = execution.get("task", {})
        task = task if isinstance(task, Mapping) else {}
        allowed_tools = tuple(
            str(name) for name in task.get("allowed_tools", ()) if str(name)
        )
        manifest = await worker_tool_manifest(self._tool_service)
        allowed_tool_set = set(allowed_tools)
        tool_specs = [
            item for item in manifest if item["name"] in allowed_tool_set
        ]
        logger.debug(
            "worker_tool_capability_snapshot",
            run_id=execution.get("run_id", ""),
            task_id=task.get("task_id", ""),
            allowed_tools=allowed_tools,
            available_tools=tuple(item["name"] for item in manifest),
            selected_tools=tuple(item["name"] for item in tool_specs),
        )
        if execution.get("_worker_result_refs"):
            tool_specs.append(
                {
                    "name": "read_worker_result",
                    "description": (
                        "Read a bounded slice from a large result produced earlier "
                        "in this Worker execution."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "result_ref": {"type": "string"},
                            "offset": {"type": "integer", "minimum": 0},
                            "max_chars": {"type": "integer", "minimum": 1},
                        },
                        "required": ["result_ref"],
                    },
                }
            )
        messages = _worker_messages(
            execution=execution,
            task=task,
            allowed_tools=allowed_tools,
            tool_specs=tool_specs,
        )
        value = await self._invoke(messages, execution)
        return _worker_result(value)

    async def _invoke(self, messages: list[Any], execution: Mapping[str, Any]) -> Any:
        task = execution.get("task", {})
        task_id = str(task.get("task_id", "")) if isinstance(task, Mapping) else ""
        generation = execution.get("execution_generation", 0)
        parent_id = f"task:{task_id}:exec:{generation}"
        return await invoke_llm_with_activity(
            self._llm,
            self._events,
            messages,
            session_id=str(execution.get("session_id", "")),
            run_id=str(execution.get("run_id", "")),
            parent_activity_id=parent_id,
        )


def _worker_messages(
    *,
    execution: Mapping[str, Any],
    task: Mapping[str, Any],
    allowed_tools: tuple[str, ...],
    tool_specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """构造包含任务输入、上游结果与可用工具的模型消息。"""
    system_message = (
        "You are an independent worker. Complete the assigned objective. "
        "You may request only the tools in the supplied tool manifest. "
        "Return one JSON object with output, evidence, uncertainties, "
        "and optionally tool_calls. Each tool call has name and arguments. "
        "When tool results are supplied, use them before requesting more. "
        f"Tool manifest: {json.dumps(tool_specs, ensure_ascii=False)}"
    )
    user_message = json.dumps(
        {
            "objective": task.get("objective", ""),
            "input": task.get("input", {}),
            "expected_output": task.get("expected_output", {}),
            "upstream": execution.get("upstream_results", {}),
            "allowed_tools": list(allowed_tools),
            "tool_results": execution.get("tool_results", []),
        },
        ensure_ascii=False,
    )
    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]


def _worker_result(value: Any) -> dict[str, Any]:
    """把 Provider 回复统一为 Worker 的结果与工具调用契约。"""
    content = value.get("content", value) if isinstance(value, Mapping) else value
    if isinstance(value, BaseMessage):
        content = value.content
    parsed = _decode_worker_reply(content)
    raw_calls = value.get("tool_calls", []) if isinstance(value, Mapping) else []
    if isinstance(value, AIMessage):
        raw_calls = value.tool_calls
    tool_calls = _normalize_tool_calls(parsed.get("tool_calls", raw_calls))
    output = parsed.get("output")
    if output is None:
        output = {} if tool_calls else {
            key: item
            for key, item in parsed.items()
            if key not in {
                "evidence",
                "uncertainties",
                "tool_calls",
                "status",
                "error",
                "retryable",
            }
        }
    if not isinstance(output, Mapping):
        output = {"value": output}
    evidence = parsed.get("evidence", [])
    if not isinstance(evidence, (list, tuple)):
        evidence = [evidence] if evidence else []
    uncertainties = parsed.get("uncertainties", [])
    if isinstance(uncertainties, str):
        uncertainties = [uncertainties] if uncertainties else []
    elif not isinstance(uncertainties, (list, tuple)):
        uncertainties = []
    result = {
        "status": str(parsed.get("status", "completed")),
        "output": output,
        "evidence": evidence,
        "uncertainties": uncertainties,
        "tool_calls": tool_calls,
    }
    if parsed.get("error") is not None:
        error = parsed["error"]
        result["error"] = (
            dict(error) if isinstance(error, Mapping) else {"message": str(error)}
        )
    if parsed.get("retryable") is not None:
        result["retryable"] = bool(parsed["retryable"])
    return result


def _decode_worker_reply(content: Any) -> dict[str, Any]:
    """Decode the Worker's JSON envelope while preserving plain-text answers."""
    if isinstance(content, Mapping):
        return dict(content)
    text = str(content or "").strip()
    provider_tool_calls = _decode_provider_tool_calls(text)
    if provider_tool_calls is not None:
        return {"tool_calls": provider_tool_calls}
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {"output": text}
    return dict(value) if isinstance(value, Mapping) else {"output": value}


def _decode_provider_tool_calls(text: str) -> list[dict[str, Any]] | None:
    """Decode Qwen-style textual function calls into the Worker JSON contract.
    The configured provider can return a tool call as ``<tool_call>`` text even
    when the prompt requests JSON. The result still goes through the same
    task allowlist, approval, and execution checks as a JSON tool call.
    """
    if "<tool_call>" not in text:
        return None
    blocks = re.findall(r"<tool_call>(.*?)</tool_call>", text, flags=re.DOTALL)
    if not blocks:
        return None
    calls: list[dict[str, Any]] = []
    for block in blocks:
        function_match = re.search(
            r"<function=([A-Za-z0-9_.:-]+)>", block, flags=re.DOTALL
        )
        if function_match is None:
            return None
        arguments: dict[str, Any] = {}
        for name, raw_value in re.findall(
            r"<parameter=([A-Za-z0-9_.:-]+)>(.*?)</parameter>",
            block,
            flags=re.DOTALL,
        ):
            if name in arguments:
                return None
            value = raw_value.strip()
            try:
                arguments[name] = json.loads(value)
            except (TypeError, ValueError):
                arguments[name] = value
        if not arguments:
            return None
        calls.append(
            {"name": function_match.group(1), "arguments": arguments}
        )
    return calls


def _normalize_tool_calls(value: Any) -> list[dict[str, Any]]:
    """Normalize JSON or LangChain tool-call records for the Worker graph."""
    if not isinstance(value, (list, tuple)):
        return []
    calls: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name", "")).strip()
        arguments = item.get("arguments", item.get("args", {}))
        if name:
            calls.append(
                {
                    "id": str(item.get("id", "")),
                    "name": name,
                    "arguments": dict(arguments)
                    if isinstance(arguments, Mapping)
                    else arguments,
                }
            )
    return calls
__all__ = ["ConfiguredWorkerAgent"]
