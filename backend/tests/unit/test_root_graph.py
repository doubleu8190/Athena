from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.orchestration import RootWorkflow, RootWorkflowDependencies
from application.orchestration.root_workflow import _merge_context_window
from application.orchestration.root_decision import (
    Answer,
    ContextRequest,
    PlanDraft,
    PlanTaskDraft,
)
from application.runs.context import (
    ContextAcquisitionService,
    ContextBundle,
    ContextItem,
    ContextPlanner,
    MemoryQuery,
    ProviderResult,
)
from application.orchestration.langgraph import build_root_graph, invoke_root_graph


class _Agent:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.inputs = []

    async def decide(self, inputs):
        self.inputs.append(inputs)
        return self.decisions.pop(0)


class _RequestPreparation:
    async def prepare(self, *, request):
        return {"request": {**request, "message_id": request["message_id"] or "message-1"}}

    async def load_history(self, *, session_id, exclude_message_id):
        return []

    async def load_attachments(self, *, session_id, attachment_ids):
        return []

    async def load_knowledge_bases(self):
        return []


class _Provider:
    def __init__(self, handler):
        self.handler = handler

    async def retrieve_context(self, *, session_id, run_id, plan):
        return await self.handler(session_id=session_id, run_id=run_id, plan=plan)


class _Events:
    def __init__(self):
        self.events = []

    async def publish(self, _event):
        self.events.append(_event)
        return None


class _TraceRepository:
    async def record_context(self, **_kwargs):
        return "retrieval-1"


class _Tools:
    async def list_tools(self):
        return []


def _dependencies(agent, *, provider=None, root_max_turns=4, events=None):
    events = events or _Events()
    providers = {"memory": _Provider(provider)} if provider else {}
    return RootWorkflowDependencies(
        context_acquisition=ContextAcquisitionService(
            providers,
            trace_repository=_TraceRepository(),
            event_publisher=events,
        ),
        context_planner=ContextPlanner(),
        request_preparation=_RequestPreparation(),
        agent=agent,
        event_publisher=events,
        tool_service=_Tools(),
        available_context_providers=tuple(providers),
        root_max_turns=root_max_turns,
    )


def _build_graph(dependencies):
    return build_root_graph(
        workflow=RootWorkflow(dependencies), dependencies=dependencies
    )


@pytest.mark.asyncio
async def test_root_decides_to_retrieve_then_uses_memory_before_answering() -> None:
    observed = {}

    async def retrieve(*, session_id, run_id, plan):
        observed.update(session_id=session_id, run_id=run_id, plan=plan)
        return ProviderResult(
            "memory",
            "succeeded",
            items=(ContextItem("memory", "fruit_preference: 苹果", "turn:old:0"),),
            result_count=1,
        )

    agent = _Agent(
        [
            ContextRequest(
                action="context",
                queries=(MemoryQuery(provider="memory", query="最喜欢的水果"),),
            ),
            Answer(action="answer", kind="final", content="你最喜欢吃苹果。"),
        ]
    )
    dependencies = _dependencies(agent, provider=retrieve)

    result = await invoke_root_graph(
        _build_graph(dependencies),
        session_id="session-1",
        run_id="run-1",
        message_id="message-1",
        user_message="你知道我最喜欢吃什么水果吗？",
    )

    assert result["status"] == "completed"
    assert observed["plan"].providers == ("memory",)
    assert len(agent.inputs) == 2
    assert agent.inputs[1].context_items[0].content == "fruit_preference: 苹果"
    assert agent.inputs[1].context_queries[0].status == "succeeded"


@pytest.mark.asyncio
async def test_repeated_root_and_context_nodes_publish_distinct_activity_ids() -> None:
    async def retrieve(*, session_id, run_id, plan):
        return ProviderResult("memory", "succeeded", result_count=0)

    events = _Events()
    request_context = ContextRequest(
        action="context",
        queries=(MemoryQuery(provider="memory", query="check latest policy"),),
    )
    agent = _Agent(
        [
            request_context,
            request_context,
            Answer(action="answer", kind="final", content="No matching policy found."),
        ]
    )
    dependencies = _dependencies(agent, provider=retrieve, events=events)

    result = await invoke_root_graph(
        _build_graph(dependencies),
        session_id="session-1",
        run_id="run-repeated-activities",
        message_id="message-repeated-activities",
        user_message="Check the latest policy.",
    )

    assert result["status"] == "completed"
    expected_invocations = {"decide_root_action": 3, "acquire_context": 2}
    for node_name, expected_count in expected_invocations.items():
        started = {
            event.payload["activity_id"]: event
            for event in events.events
            if event.event_type == "node.started"
            and event.payload["node"] == node_name
        }
        completed = {
            event.payload["activity_id"]: event
            for event in events.events
            if event.event_type == "node.completed"
            and event.payload["node"] == node_name
        }

        assert len(started) == expected_count
        assert set(started) == set(completed)
        assert all(
            event.payload["parent_activity_id"] == "run:run-repeated-activities"
            for event in started.values()
        )
        assert all(
            started[activity_id].occurred_at <= completed[activity_id].occurred_at
            for activity_id in started
        )


def test_root_graph_has_no_removed_understanding_or_harness_nodes() -> None:
    dependencies = _dependencies(_Agent([Answer(action="answer", kind="final", content="ok")]))
    node_names = set(_build_graph(dependencies).get_graph().nodes)

    assert "decide_root_action" in node_names
    assert "acquire_context" in node_names
    assert "wait_for_plan_completion" in node_names
    assert "build_plan_response" in node_names
    assert "assemble_final_response" in node_names
    assert not {
        "understand_task",
        "plan_context",
        "prepare_harness_input",
        "run_root_execution_loop",
    } & node_names


@pytest.mark.asyncio
async def test_root_workflow_loads_plan_results_once_and_renders_response() -> None:
    dependencies = _dependencies(_Agent([]))
    load_calls = 0

    async def load_plan(*, plan_id):
        nonlocal load_calls
        load_calls += 1
        return {"completed": True, "results": {"task-1": {"output": "done"}}}

    dependencies.plan_loader = load_plan
    workflow = RootWorkflow(dependencies)
    state = {
        "request": {"run_id": "run-1"},
        "plan": {
            "plan_id": "plan-1",
            "tasks": [{"task_id": "task-1", "title": "Task one"}],
        },
    }

    wait_result = await workflow.wait_for_plan_completion(state)
    response_result = await workflow.build_plan_response({**state, **wait_result})
    final_result = await workflow.assemble_final_response({**state, **response_result})

    assert load_calls == 1
    assert response_result == {
        "response": {"content": "Task one：done"},
        "waiting": False,
    }
    assert final_result == {
        "result": {"content": "Task one：done"},
        "status": "completed",
        "waiting": False,
    }


@pytest.mark.asyncio
async def test_root_workflow_assembles_node_error_as_failed_result() -> None:
    workflow = RootWorkflow(_dependencies(_Agent([])))

    result = await workflow.assemble_final_response(
        {"error": {"code": "node_failed", "message": "failed"}}
    )

    assert result == {
        "result": {"error": {"code": "node_failed", "message": "failed"}},
        "status": "failed",
        "waiting": False,
    }


@pytest.mark.asyncio
async def test_root_graph_routes_clarification_from_unified_answer_decision() -> None:
    agent = _Agent(
        [Answer(action="answer", kind="clarification", content="请选择目标文件。")]
    )
    dependencies = _dependencies(agent)

    result = await invoke_root_graph(
        _build_graph(dependencies),
        session_id="session-1",
        run_id="run-2",
        message_id="message-2",
        user_message="处理文件",
    )

    assert result["result"] == {
        "content": "请选择目标文件。",
        "kind": "clarification",
    }


@pytest.mark.asyncio
async def test_root_budget_stops_after_last_context_decision_without_extra_retrieval() -> None:
    provider_calls = 0

    async def retrieve(*, session_id, run_id, plan):
        nonlocal provider_calls
        provider_calls += 1
        return ProviderResult("memory", "empty")

    request_context = ContextRequest(
        action="context",
        queries=(MemoryQuery(provider="memory", query="same query"),),
    )
    agent = _Agent([request_context, request_context])
    dependencies = _dependencies(agent, provider=retrieve, root_max_turns=2)

    result = await invoke_root_graph(
        _build_graph(dependencies),
        session_id="session-1",
        run_id="run-budget",
        message_id="message-budget",
        user_message="查找资料",
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "root_budget_exhausted"
    assert provider_calls == 1
    assert len(agent.inputs) == 2


@pytest.mark.asyncio
async def test_root_allows_plan_on_last_decision_turn() -> None:
    agent = _Agent(
        [
            PlanDraft(
                action="plan",
                goal="列出目录文件",
                tasks=(
                    PlanTaskDraft(
                        task_id="list-files",
                        title="列出文件",
                        objective="列出指定目录中的文件名",
                        allowed_tools=(),
                        input={"directory": "/tmp"},
                        expected_output={"format": "file names"},
                    ),
                ),
            )
        ]
    )
    workflow = RootWorkflow(_dependencies(agent, root_max_turns=1))

    result = await workflow.decide_root_action(
        {
            "request": {
                "session_id": "session-last-turn",
                "run_id": "run-last-turn",
                "message_id": "message-last-turn",
                "user_message": "列出目录文件",
                "history": [],
                "attachment_refs": [],
            },
            "knowledge_bases": [],
            "worker_tools": [],
            "available_context_providers": [],
            "context_bundle": {"items": [], "provider_results": []},
            "context_queries": [],
            "root_turn_count": 0,
        }
    )

    assert agent.inputs[0].remaining_turns == 1
    assert result["root_turn_count"] == 1
    assert result["root_decision"]["action"] == "plan"
    assert "error" not in result


def test_context_window_prefers_new_sources_and_respects_both_limits() -> None:
    previous = ContextBundle(
        items=(
            ContextItem("memory", "old version", "source-1"),
            ContextItem("memory", "duplicate text"),
            ContextItem("file", "old file", "file-1"),
        ),
        provider_results=(),
    )
    current = ContextBundle(
        items=(
            ContextItem("memory", "new version", "source-1"),
            ContextItem("memory", "duplicate text"),
            ContextItem("graph", "new graph", "graph-1"),
        ),
        provider_results=(),
    )

    merged = _merge_context_window(
        previous,
        current,
        max_items=3,
        max_tokens=6,
        token_counter=lambda text: len(text.split()),
    )

    assert [(item.provider, item.content) for item in merged.items] == [
        ("memory", "new version"),
        ("memory", "duplicate text"),
        ("graph", "new graph"),
    ]
    assert merged.truncated


def test_context_window_uses_configured_tokenizer_for_multilingual_content() -> None:
    planner = ContextPlanner(
        max_tokens=4,
        token_counter=lambda text: 5 if text == "中文资料" else 1,
    )

    merged = _merge_context_window(
        ContextBundle(items=(), provider_results=()),
        ContextBundle(
            items=(ContextItem("memory", "中文资料"),),
            provider_results=(),
        ),
        max_items=12,
        max_tokens=planner.max_tokens,
        token_counter=planner.count_tokens,
    )

    assert merged.items == ()
    assert merged.truncated


@pytest.mark.asyncio
async def test_unavailable_provider_result_is_returned_to_root_for_next_decision() -> None:
    agent = _Agent(
        [
            ContextRequest(
                action="context",
                queries=(MemoryQuery(provider="memory", query="old memory"),),
            ),
            Answer(action="answer", kind="final", content="I cannot access memory now."),
        ]
    )
    dependencies = _dependencies(agent)

    result = await invoke_root_graph(
        _build_graph(dependencies),
        session_id="session-1",
        run_id="run-unavailable",
        message_id="message-unavailable",
        user_message="查找记忆",
    )

    assert result["status"] == "completed"
    assert agent.inputs[1].context_queries[0].status == "skipped"
    assert agent.inputs[1].context_queries[0].error_message == "context_provider_unavailable"


@pytest.mark.asyncio
async def test_root_rejects_legacy_state_based_resume() -> None:
    with pytest.raises(RuntimeError, match="root_resume_value_type_invalid"):
        await invoke_root_graph(
            None,
            session_id="session-1",
            run_id="run-legacy-resume",
            user_message="",
            resume_value={"tool_call_id": "old-root-approval"},
        )
