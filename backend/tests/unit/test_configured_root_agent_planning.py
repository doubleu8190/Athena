"""Root 统一决策解析与有界格式修复测试。"""

from __future__ import annotations

import json
import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from application.orchestration.agents.root_agent import (
    ConfiguredRootAgent,
    QueryRewriteOutput,
    RootDecisionFormatError,
)
from application.orchestration.root_decision import (
    AnswerIntent,
    PlanDraft,
    PlanIntent,
    RootPlanInput,
    ROOT_DECISION_ADAPTER,
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
async def test_root_query_rewrite_uses_second_model_and_dialogue_context() -> None:
    class SecondaryLLM:
        def __init__(self) -> None:
            self.messages = None
            self.schema = None

        async def invoke_secondary_structured(self, messages, schema) -> object:
            self.messages = messages
            self.schema = schema
            return {"rewritten_query": "苹果的保存期限"}

    llm = SecondaryLLM()
    agent = ConfiguredRootAgent(llm)
    rewritten = await agent.rewrite_query(
        "这个能放多久？",
        (
            RootMessage(role="user", content="我买了苹果。"),
            RootMessage(role="user", content="这个能放多久？"),
        ),
        session_id="session-1",
        run_id="run-1",
    )

    assert rewritten == "苹果的保存期限"
    assert llm.schema is QueryRewriteOutput
    assert "我买了苹果" in llm.messages[1]["content"]
    assert "这个能放多久" in llm.messages[1]["content"]
    assert "不得解释、回答、猜测" in llm.messages[0]["content"]


@pytest.mark.asyncio
async def test_root_query_rewrite_without_history_uses_current_question() -> None:
    class SecondaryLLM:
        async def invoke_secondary_structured(self, messages, _schema):
            assert "对话历史：\n无" in messages[1]["content"]
            assert "当前问题：\n今天下雨吗？" in messages[1]["content"]
            return QueryRewriteOutput(rewritten_query="今天是否下雨")

    result = await ConfiguredRootAgent(SecondaryLLM()).rewrite_query(
        "今天下雨吗？", (), session_id="session-1", run_id="run-1"
    )

    assert result == "今天是否下雨"


@pytest.mark.asyncio
async def test_root_query_rewrite_records_model_activity_without_query_content() -> None:
    class SecondaryLLM:
        async def invoke_secondary_structured(self, _messages, _schema):
            return {"rewritten_query": "改写问题"}

    class Events:
        def __init__(self):
            self.events = []

        async def publish(self, event):
            self.events.append(event)
            return event

    events = Events()
    agent = ConfiguredRootAgent(SecondaryLLM(), events)
    await agent.rewrite_query(
        "机密原始问题", (), session_id="session-1", run_id="run-1"
    )

    assert [event.event_type for event in events.events] == [
        "llm.started",
        "llm.completed",
    ]
    assert all("机密原始问题" not in str(event.payload) for event in events.events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"rewritten_query": ""},
        {"rewritten_query": " "},
        {"rewritten_query": "x" * 501},
        {"rewritten_query": "有效查询", "explanation": "额外字段"},
        {"rewritten_query": 123},
        "非结构化文字",
    ],
)
async def test_root_query_rewrite_rejects_invalid_structured_output(response) -> None:
    class SecondaryLLM:
        async def invoke_secondary_structured(self, _messages, _schema):
            return response

    with pytest.raises(ValueError):
        await ConfiguredRootAgent(SecondaryLLM()).rewrite_query(
            "当前问题", (), session_id="session-1", run_id="run-1"
        )


@pytest.mark.asyncio
async def test_root_query_rewrite_propagates_cancellation() -> None:
    class SecondaryLLM:
        async def invoke_secondary_structured(self, _messages, _schema):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await ConfiguredRootAgent(SecondaryLLM()).rewrite_query(
            "当前问题", (), session_id="session-1", run_id="run-1"
        )


@pytest.mark.asyncio
async def test_initial_decision_prompt_uses_prefetched_context_and_has_no_context_action() -> None:
    llm = _SequenceLLM(
        [{"action": "answer", "kind": "final", "instructions": "回答问题。"}]
    )
    await ConfiguredRootAgent(llm).decide(_inputs())

    prompt = llm.messages[0][0]["content"]
    assert "原始用户 query" in prompt
    assert "改写后的 query" in prompt
    assert "只选择直接回答、向用户澄清或生成执行计划" in prompt
    assert "`context`：" not in prompt


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

    with pytest.raises(ValidationError):
        ROOT_DECISION_ADAPTER.validate_python({"action": "context", "queries": []})


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
