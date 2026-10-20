"""Default Root Agent bridge for configured language models."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any


class ConfiguredRootAgent:
    """Translate Root harness input into one configured LLM invocation."""

    def __init__(self, llm: Any) -> None:
        """保存 LLM provider；不在构造阶段建立外部连接。"""
        self._llm = llm

    async def __call__(self, *, harness_input: Mapping[str, Any], **_kwargs: Any) -> dict[str, Any]:
        """执行一轮 Root LLM 调用并返回 JSON-safe 响应。"""
        messages = list(harness_input.get("messages", []))
        context = harness_input.get("context", {})
        task = harness_input.get("task", {})
        planning = isinstance(task, Mapping) and task.get("mode") == "plan"
        if planning:
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": (
                        "Return only valid JSON for a parallel execution plan. "
                        "Use keys plan_id, goal, max_parallelism, edges, and tasks. "
                        "Each task must have task_id, title, objective, worker_type, "
                        "allowed_tools, input, and expected_output. Use worker_type "
                        "'general' for every task and an empty allowed_tools array; "
                        "only registered tools may be used. Keep the DAG acyclic."
                    ),
                },
            )
        if isinstance(context, Mapping) and context.get("items"):
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": "Use the supplied context as reference material.",
                },
            )
        value = await self._llm.invoke(messages)
        if isinstance(value, Mapping):
            content = value.get("content", value.get("result", value))
        else:
            content = getattr(value, "content", value)
        if planning:
            payload = self._decode_plan(content)
            if payload is not None:
                return {"plan": payload, "next_route": "materialize_execution_plan"}
        return {"response": {"content": str(content)}}

    @staticmethod
    def _decode_plan(value: Any) -> dict[str, Any] | None:
        if isinstance(value, Mapping):
            candidate = value.get("plan", value)
            return dict(candidate) if isinstance(candidate, Mapping) else None
        text = str(value).strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:].lstrip()
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        if isinstance(parsed, Mapping):
            candidate = parsed.get("plan", parsed)
            return dict(candidate) if isinstance(candidate, Mapping) else None
        return None


class ConfiguredWorkerAgent:
    """Run one persisted task with the configured model."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    async def __call__(self, *, execution: Mapping[str, Any], **_kwargs: Any) -> dict[str, Any]:
        task = execution.get("task", {})
        upstream = execution.get("upstream_results", {})
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an independent worker. Complete the assigned objective. "
                    "Return concise JSON with keys output and evidence."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"objective": task.get("objective", ""), "input": task.get("input", {}), "upstream": upstream},
                    ensure_ascii=False,
                ),
            },
        ]
        value = await self._llm.invoke(messages)
        content = value.get("content", value) if isinstance(value, Mapping) else getattr(value, "content", value)
        try:
            parsed = json.loads(str(content).strip().strip("`"))
        except (TypeError, ValueError):
            parsed = {"output": str(content)}
        if not isinstance(parsed, Mapping):
            parsed = {"output": parsed}
        return {"status": "done", "output": dict(parsed), "evidence": parsed.get("evidence", [])}


__all__ = ["ConfiguredRootAgent", "ConfiguredWorkerAgent"]
