"""Root 统一决策解析与有界格式修复测试。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from application.orchestration.agents.root_agent import (
    ConfiguredRootAgent,
    RootDecisionFormatError,
)
from application.orchestration.plan_versions import partition_plan_versions
from application.orchestration.root_decision import (
    Answer,
    ContextRequest,
    PlanDraft,
    RootAgentInput,
    RootDecisionStructuredOutput,
    RootMessage,
)
from application.runs.context import MemoryQuery
from application.orchestration.agents.worker_agent import _decode_worker_reply


class _SequenceLLM:
    def __init__(self, values: list[object]) -> None:
        self.values = values
        self.messages: list[list[dict[str, Any]]] = []
        self.schemas: list[object] = []

    async def invoke(self, messages: list[dict[str, Any]]) -> object:
        self.messages.append(messages)
        return self.values[len(self.messages) - 1]

    async def invoke_structured(
        self,
        messages: list[dict[str, Any]],
        schema: object,
    ) -> object:
        self.messages.append(messages)
        self.schemas.append(schema)
        return self.values[len(self.messages) - 1]


def _inputs() -> RootAgentInput:
    return RootAgentInput(
        session_id="session-1",
        run_id="run-1",
        message_id="message-1",
        messages=(RootMessage(role="user", content="Complete one task"),),
        remaining_turns=4,
    )


def _plan(*, explicit_version: bool = False) -> dict[str, Any]:
    task = {
        "task_id": "task-1",
        "title": "Complete task",
        "objective": "Return a result",
        "allowed_tools": [],
        "input": {"goal": "Complete one task"},
        "expected_output": {"format": "text"},
    }
    if explicit_version:
        task["plan_version"] = 1
    return {
        "action": "plan",
        "goal": "Complete one task",
        "max_parallelism": 1,
        "edges": [],
        "tasks": [task],
    }


@pytest.mark.asyncio
async def test_root_agent_uses_structured_output_for_plan_decision() -> None:
    llm = _SequenceLLM([_plan()])

    result = await ConfiguredRootAgent(llm).decide(_inputs())

    assert isinstance(result, PlanDraft)
    assert result.tasks[0].task_id == "task-1"
    assert result.tasks[0].input == {"goal": "Complete one task"}
    assert llm.schemas == [RootDecisionStructuredOutput]


@pytest.mark.asyncio
async def test_root_agent_repairs_invalid_decision_once() -> None:
    llm = _SequenceLLM([{"action": "answer"}, _plan()])

    result = await ConfiguredRootAgent(llm).decide(_inputs())

    assert isinstance(result, PlanDraft)
    assert len(llm.messages) == 2
    assert "Validation error" in llm.messages[1][-1]["content"]
    assert '"action": "answer"' in llm.messages[1][-2]["content"]


@pytest.mark.asyncio
async def test_root_agent_raises_after_one_invalid_repair() -> None:
    llm = _SequenceLLM([{"action": "answer"}, {"action": "answer"}])

    with pytest.raises(RootDecisionFormatError, match="root_decision_invalid_after_repair"):
        await ConfiguredRootAgent(llm).decide(_inputs())

    assert len(llm.messages) == 2


def test_root_agent_accepts_only_complete_json_or_a_single_outer_fence() -> None:
    fenced = "```json\n" + json.dumps(_plan()) + "\n```"

    decision = ConfiguredRootAgent._decode_decision(fenced)

    assert isinstance(decision, PlanDraft)
    with pytest.raises(ValueError, match="root_decision_invalid_json"):
        ConfiguredRootAgent._decode_decision("Plan: " + json.dumps(_plan()))


def test_root_decision_parser_keeps_explicit_task_version_for_materialization() -> None:
    decision = ConfiguredRootAgent._decode_decision(json.dumps(_plan(explicit_version=True)))

    assert isinstance(decision, PlanDraft)
    assert "plan_version" in decision.tasks[0].model_fields_set


def test_plan_version_partition_preserves_only_first_version_for_submission() -> None:
    plan = {
        "action": "plan",
        "goal": "Draft then review",
        "tasks": [
            {
                "task_id": "draft",
                "title": "Draft",
                "objective": "Write draft",
                "plan_version": 1,
                "allowed_tools": [],
                "input": {"topic": "x"},
                "expected_output": {"format": "text"},
            },
            {
                "task_id": "review",
                "title": "Review",
                "objective": "Review draft",
                "plan_version": 2,
                "allowed_tools": [],
                "depends_on": ["draft"],
                "input": {"topic": "x"},
                "expected_output": {"format": "review"},
            },
        ],
        "edges": [],
    }

    current, deferred = partition_plan_versions(plan)

    assert [task["task_id"] for task in current["tasks"]] == ["draft"]
    assert current["edges"] == []
    assert deferred["2"][0]["depends_on"] == ["draft"]


def test_plan_version_partition_rejects_dependency_on_a_later_version() -> None:
    plan = {
        "tasks": [
            {"task_id": "draft", "plan_version": 1},
            {"task_id": "review", "plan_version": 2, "depends_on": ["later"]},
            {"task_id": "later", "plan_version": 3},
        ],
        "edges": [],
    }

    with pytest.raises(ValueError, match="root_plan_version_dependency_invalid"):
        partition_plan_versions(plan)


def test_root_contract_rejects_blank_required_text_and_duplicate_providers() -> None:
    with pytest.raises(ValidationError, match="root_answer_content_required"):
        Answer(action="answer", kind="final", content="  ")

    with pytest.raises(ValidationError, match="root_plan_goal_required"):
        PlanDraft(action="plan", goal=" ", tasks=(_plan()["tasks"][0],))

    with pytest.raises(ValidationError, match="one_query_per_context_provider"):
        ContextRequest(
            action="context",
            queries=(
                MemoryQuery(provider="memory", query="first"),
                MemoryQuery(provider="memory", query="second"),
            ),
        )


def test_worker_reply_decodes_provider_textual_tool_call() -> None:
    response = (
        "<tool_call><function=exec_shell>"
        "<parameter=command>python -c 'import sys; "
        "print(sys.version.split()[0])'</parameter>"
        "<parameter=timeout>60</parameter>"
        "</function></tool_call>"
    )

    decoded = _decode_worker_reply(response)

    assert decoded == {
        "tool_calls": [
            {
                "name": "exec_shell",
                "arguments": {
                    "command": "python -c 'import sys; print(sys.version.split()[0])'",
                    "timeout": 60,
                },
            }
        ]
    }
