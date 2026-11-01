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
from application.orchestration.root_decision import (
    AnswerIntent,
    ContextRequest,
    PlanDraft,
    PlanIntent,
    RootPlanInput,
    ROOT_PLAN_ADAPTER,
    RootAgentInput,
    RootDecisionStructuredOutput,
    RootPlanStructuredOutput,
    RootMessage,
)
from application.runs.context import ContextItem, ContextQueryOutcome, MemoryQuery
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
        "task_ref": "task-1",
        "contract_id": "worker.text",
        "contract_revision": 1,
        "objective": "Return a result",
        "input": {"goal": "Complete one task"},
        "acceptance_criteria": "Return non-empty text",
    }
    if explicit_version:
        task["plan_version"] = 1
    return {
        "action": "plan",
        "goal": "Complete one task",
        "tasks": [task],
    }


def _plan_intent() -> dict[str, str]:
    return {
        "action": "plan",
        "goal": "Complete one task",
        "instructions": "Create a plan to complete one task.",
    }


def _plan_inputs() -> RootPlanInput:
    inputs = _inputs()
    return RootPlanInput(
        session_id=inputs.session_id,
        run_id=inputs.run_id,
        message_id=inputs.message_id,
        messages=inputs.messages,
        goal="Complete one task",
        instructions="Create a plan to complete one task.",
    )


@pytest.mark.asyncio
async def test_root_agent_uses_separate_structured_calls_for_plan_intent_and_draft() -> None:
    llm = _SequenceLLM([_plan_intent(), _plan()])
    agent = ConfiguredRootAgent(llm)

    intent = await agent.decide(_inputs())
    result = await agent.plan(_plan_inputs())

    assert isinstance(intent, PlanIntent)
    assert isinstance(result, PlanDraft)
    assert result.tasks[0].task_ref == "task-1"
    assert result.tasks[0].input == {"goal": "Complete one task"}
    assert llm.schemas == [RootDecisionStructuredOutput, RootPlanStructuredOutput]


@pytest.mark.asyncio
async def test_plan_serializes_retrieved_dataclass_material_and_query_outcomes() -> None:
    """非空检索资料进入真实计划消息构造，不因 dataclass 序列化而失败。

    参数：无。返回：无。异常：序列化错误或内容丢失使断言失败。
    """
    llm = _SequenceLLM([_plan()])
    inputs = _plan_inputs().model_copy(update={
        "context_items": (ContextItem("memory", "报告使用中文", "preference", {"category": "preference"}),),
        "context_queries": (ContextQueryOutcome(
            stage="prefetch", query=MemoryQuery(provider="memory", query="报告"),
            status="succeeded", result_count=1,
        ),),
    })
    result = await ConfiguredRootAgent(llm).plan(inputs)
    assert isinstance(result, PlanDraft)
    payload = json.loads(llm.messages[0][1]["content"])
    assert payload["context_items"][0] == {
        "provider": "memory", "content": "报告使用中文", "source_id": "preference",
        "metadata": {"category": "preference"},
    }
    assert payload["context_queries"][0]["stage"] == "prefetch"


@pytest.mark.asyncio
async def test_root_agent_repairs_invalid_decision_once() -> None:
    llm = _SequenceLLM([{"action": "answer"}, _plan_intent()])

    result = await ConfiguredRootAgent(llm).decide(_inputs())

    assert isinstance(result, PlanIntent)
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
    fenced = "```json\n" + json.dumps(_plan_intent()) + "\n```"

    decision = ConfiguredRootAgent._decode_decision(fenced)

    assert isinstance(decision, PlanIntent)
    with pytest.raises(ValueError, match="root_decision_invalid_json"):
        ConfiguredRootAgent._decode_decision("Plan: " + json.dumps(_plan_intent()))


def test_root_plan_rejects_dag_edges_and_explicit_future_version() -> None:
    with pytest.raises(ValidationError):
        ROOT_PLAN_ADAPTER.validate_python(_plan(explicit_version=True))

    with pytest.raises(ValidationError):
        ROOT_PLAN_ADAPTER.validate_python(
            {
                **_plan(),
                "edges": [{"from_task_id": "a", "to_task_id": "b"}],
            }
        )


@pytest.mark.parametrize("response_type", [dict, RootPlanStructuredOutput])
def test_root_plan_decoder_filters_known_other_action_fields(response_type: type) -> None:
    response = {
        **_plan(),
        "missing_information": None,
        "reason": None,
    }
    value = (
        response_type(**response)
        if response_type is RootPlanStructuredOutput
        else response
    )
    result = ConfiguredRootAgent._decode_plan(value)
    assert isinstance(result, PlanDraft)


@pytest.mark.parametrize("response_type", [dict, RootPlanStructuredOutput])
def test_root_plan_decoder_filters_plan_fields_for_information_feedback(
    response_type: type,
) -> None:
    response = {
        "action": "needs_information",
        "missing_information": ["worker input"],
        "reason": "required",
        "goal": "irrelevant for this action",
        "tasks": [],
    }
    value = (
        response_type(**response)
        if response_type is RootPlanStructuredOutput
        else response
    )
    result = ConfiguredRootAgent._decode_plan(value)
    assert result.action == "needs_information"


def test_root_plan_decoder_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ConfiguredRootAgent._decode_plan({**_plan(), "unexpected": True})


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_type", [dict, RootPlanStructuredOutput])
async def test_root_plan_repair_uses_action_specific_decoder(
    repair_type: type,
) -> None:
    # 未知字段必须触发一次修复；修复响应则验证已知跨 action 字段会被清理。
    repaired = {**_plan(), "missing_information": None, "reason": None}
    repaired_value = (
        repair_type(**repaired)
        if repair_type is RootPlanStructuredOutput
        else repaired
    )
    llm = _SequenceLLM([{**_plan(), "unexpected": True}, repaired_value])
    result = await ConfiguredRootAgent(llm).plan(_plan_inputs())

    assert isinstance(result, PlanDraft)
    assert len(llm.messages) == 2
    assert "Validation error" in llm.messages[1][-1]["content"]


def test_root_contract_rejects_blank_required_text_and_duplicate_providers() -> None:
    with pytest.raises(ValidationError, match="root_answer_instructions_required"):
        AnswerIntent(action="answer", kind="final", instructions="  ")

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
