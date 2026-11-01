from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from application.orchestration import RootWorkflow, RootWorkflowDependencies
from application.orchestration.root_workflow import (
    _merge_context_window,
    render_plan_results,
)
from application.orchestration.root_decision import (
    AnswerIntent,
    ContextRequest,
    PlanIntent,
    PlanningFeedback,
    PlanDraft,
)
from application.orchestration.answer_generation import (
    AnswerGenerationResult,
    AnswerStreamMetadata,
)
from application.runs.context import (
    ContextAcquisitionService,
    ContextBundle,
    ContextItem,
    ContextPlanner,
    MemoryQuery,
    ProviderResult,
)
from domain.orchestration import RootPlanFacts, RootPlanTaskFact
from domain.runs import RootPlanCheckpointRecovery, RootPlanCompletedResumeValue
from application.runs.request_contracts import RootRequestState
from application.orchestration.langgraph import build_root_graph
from application.orchestration.langgraph import invoke_root_graph as _invoke_root_graph
from application.orchestration.langgraph.runtime.root_adapter import LangGraphRootGraph
from tests.fakes.leases import lease_context, lease_grant


async def invoke_root_graph(graph, **kwargs):
    """Attach a synthetic root grant to direct graph unit invocations."""
    kwargs.setdefault(
        "lease", lease_grant("root_execution", str(kwargs["run_id"]))
    )
    return await _invoke_root_graph(graph, **kwargs)
from domain.memory import MemoryRecord


class _Agent:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.inputs = []

    async def decide(self, inputs):
        self.inputs.append(inputs)
        return self.decisions.pop(0)

    async def redecide(self, inputs):
        self.inputs.append(inputs)
        return self.decisions.pop(0)

    async def plan(self, inputs):
        self.inputs.append(inputs)
        return self.decisions.pop(0)


class _RequestPreparation:
    async def prepare(self, *, request: RootRequestState) -> RootRequestState:
        return request.model_copy(
            update={"message_id": request.message_id or "message-1"}
        )

    async def load_history(self, *, session_id, exclude_message_id, before_sequence=None):
        return []

    async def load_attachments(self, *, session_id, attachment_ids):
        return []

    async def load_knowledge_bases(self):
        return []


class _Provider:
    def __init__(self, handler, memory_repository=None):
        self.handler = handler
        self.memory_repository = memory_repository

    async def retrieve_context(self, *, session_id, run_id, plan):
        result = await self.handler(session_id=session_id, run_id=run_id, plan=plan)
        if result.provider != "memory" or self.memory_repository is None:
            return result
        items = []
        for item in result.items:
            if item.source_id is None:
                items.append(item)
                continue
            metadata = {"category": "preference", **item.metadata}
            self.memory_repository.items[item.source_id] = MemoryRecord(
                item.source_id, item.content, metadata
            )
            items.append(replace(item, metadata=metadata))
        return replace(result, items=tuple(items))


class _MemoryValidationRepository:
    """为 Root 流程测试提供可复核的合成记忆来源。"""

    def __init__(self):
        self.items = {}

    async def get(self, memory_id):
        return self.items.get(memory_id)

    async def list_fact_group_by_identity(self, _identity):
        return []

    async def record_access(self, memory_ids):
        return len(memory_ids)


class _Events:
    def __init__(self):
        self.events = []

    async def publish(self, _event):
        self.events.append(_event)
        return None


class _AnswerGeneration:
    def __init__(self):
        self.inputs = []

    async def generate(self, inputs, *, stop_signal=None):
        self.inputs.append(inputs)
        content = (
            render_plan_results(inputs.plan_results)
            if inputs.plan_results is not None
            else inputs.instructions
        )
        return AnswerGenerationResult(
            content=content,
            kind=inputs.kind,
            answer_stream=AnswerStreamMetadata(
                message_id=f"assistant:{inputs.run_id}",
                stream_id=f"answer-{inputs.run_id}",
                started_at=datetime.now(timezone.utc),
                last_chunk_id=1,
            ),
        )


class _TraceRepository:
    async def record_context(self, **_kwargs):
        return "retrieval-1"

    async def update_context_selection(self, **_kwargs):
        return True


class _Tools:
    async def list_tools(self):
        return []


class _RootOrchestration:
    def __init__(self):
        self.completed_plan = None
        self.submissions = []

    async def list_authorized_capabilities(self, _run_id):
        return ()

    async def submit_plan(self, submission, *, lease):
        self.submissions.append((submission, lease))
        return SimpleNamespace(plan_id=submission.plan_id, sequence=submission.sequence)

    async def read_completed_plan(self, run_id, plan_id, root_wait_generation):
        if self.completed_plan is None:
            return None
        assert self.completed_plan.run_id == run_id
        assert self.completed_plan.plan_id == plan_id
        assert self.completed_plan.root_wait_generation == root_wait_generation
        return self.completed_plan


class _AutoCompleteRootOrchestration(_RootOrchestration):
    """在图测试中根据提交内容生成确定的完成事实。"""

    def __init__(self, outcomes: Sequence[tuple[str, Mapping[str, Any]]]) -> None:
        super().__init__()
        self.outcomes = list(outcomes)

    async def submit_plan(self, submission, *, lease):
        receipt = await super().submit_plan(submission, lease=lease)
        status, outputs = self.outcomes.pop(0)
        self.completed_plan = RootPlanFacts(
            run_id=submission.run_id,
            plan_id=submission.plan_id,
            sequence=submission.sequence,
            root_wait_generation=submission.sequence,
            goal=submission.goal,
            status="completed",
            tasks=tuple(
                RootPlanTaskFact(
                    task_ref=task.task_ref,
                    status=status,
                    output=outputs.get(task.task_ref),
                    error=(
                        {"code": "source_unavailable"}
                        if status == "failed"
                        else None
                    ),
                    objective=task.objective,
                    acceptance_criteria=task.acceptance_criteria,
                )
                for task in submission.tasks
            ),
        )
        return receipt


def _dependencies(agent, *, provider=None, root_max_turns=4, events=None):
    events = events or _Events()
    memory_repository = _MemoryValidationRepository()
    providers = {"memory": _Provider(provider, memory_repository)} if provider else {}
    dependencies = RootWorkflowDependencies(
        context_acquisition=ContextAcquisitionService(
            providers,
            trace_repository=_TraceRepository(),
            event_publisher=events,
            memory_repository=memory_repository,
        ),
        context_planner=ContextPlanner(),
        request_preparation=_RequestPreparation(),
        agent=agent,
        event_publisher=events,
        answer_generation=_AnswerGeneration(),
        tool_service=_Tools(),
        available_context_providers=tuple(providers),
        root_max_turns=root_max_turns,
    )
    dependencies.root_orchestration = _RootOrchestration()
    return dependencies


def _build_graph(dependencies, *, checkpointer=None):
    return build_root_graph(
        workflow=RootWorkflow(dependencies),
        dependencies=dependencies,
        checkpointer=checkpointer,
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
            AnswerIntent(
                action="answer", kind="final", instructions="你最喜欢吃苹果。"
            ),
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
    assert agent.inputs[0].context_items[0].content == "fruit_preference: 苹果"
    assert all(outcome.stage == "prefetch" for outcome in agent.inputs[0].context_queries)
    assert agent.inputs[1].context_queries[-1].status == "succeeded"
    assert agent.inputs[1].context_queries[-1].stage == "directed"


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
            PlanIntent(
                action="plan",
                goal="检索政策并判断是否需要更新",
                instructions="需要时生成计划。",
            ),
            PlanningFeedback(
                action="needs_information",
                missing_information=("最新政策内容",),
                reason="尚未取得所需的政策资料。",
            ),
            request_context,
            AnswerIntent(
                action="answer", kind="final", instructions="No matching policy found."
            ),
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
    expected_invocations = {
        "prefetch_context": 1,
        "decide_root_action": 2,
        "redecide_root_action": 2,
        "generate_plan": 1,
        "acquire_context": 2,
    }
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
    dependencies = _dependencies(
        _Agent([AnswerIntent(action="answer", kind="final", instructions="ok")])
    )
    node_names = set(_build_graph(dependencies).get_graph().nodes)

    assert "decide_root_action" in node_names
    assert "prefetch_context" in node_names
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
    dependencies.root_orchestration.completed_plan = RootPlanFacts(
        run_id="run-1",
        plan_id="plan-1",
        sequence=1,
        root_wait_generation=1,
        goal="Summarize plan results",
        status="completed",
        tasks=(
            RootPlanTaskFact(
                task_ref="task-1",
                status="succeeded",
                output={"content": "done"},
                objective="Produce the summary",
                acceptance_criteria="Return a non-empty summary",
            ),
        ),
    )
    workflow = RootWorkflow(dependencies)
    state = {
        "execution_lease": lease_context(lease_grant("root_execution", "run-1")),
        "request": {
            "run_id": "run-1",
            "session_id": "session-1",
            "message_id": "message-1",
            "user_message": "Summarize the plan results",
        },
        "plan": {
            "plan_id": "plan-1",
            "sequence": 1,
            "root_wait_generation": 1,
            "tasks": [{"task_ref": "task-1", "objective": "Task one"}],
        },
    }

    wait_result = await workflow.wait_for_plan_completion(state)
    assert wait_result["plan_results"]["tasks"][0]["objective"] == "Produce the summary"
    assert wait_result["plan_results"]["tasks"][0]["acceptance_criteria"] == (
        "Return a non-empty summary"
    )
    response_result = await workflow.build_plan_response({**state, **wait_result})

    assert response_result == {
        "answer_input": {
            "source": "plan_summary",
            "kind": "final",
            "instructions": "根据用户原始目标总结已完成的任务结果，准确保留失败项、不确定性和未执行事实。",
        },
        "waiting": False,
    }
    answer_state = {**state, **wait_result, **response_result}
    answer_result = await workflow.generate_answer(answer_state)
    final_result = await workflow.assemble_final_response(
        {**answer_state, **answer_result}
    )
    assert final_result == {
        "result": {
            "content": "task-1：done",
            "kind": "final",
            "answer_stream": answer_result["response"]["answer_stream"],
        },
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
        [
            AnswerIntent(
                action="answer",
                kind="clarification",
                instructions="请用户选择目标文件。",
            )
        ]
    )
    dependencies = _dependencies(agent)

    result = await invoke_root_graph(
        _build_graph(dependencies),
        session_id="session-1",
        run_id="run-2",
        message_id="message-2",
        user_message="处理文件",
    )

    assert result["result"]["content"] == "请用户选择目标文件。"
    assert result["result"]["kind"] == "clarification"


@pytest.mark.asyncio
async def test_root_graph_answers_when_completed_facts_satisfy_requested_goal() -> None:
    task = {
        "task_ref": "report",
        "contract_id": "worker.report",
        "contract_revision": 1,
        "objective": "Prepare the requested report",
        "input": {"topic": "quarterly summary"},
        "acceptance_criteria": "Return the completed report text",
    }
    agent = _Agent(
        [
            PlanIntent(
                action="plan",
                goal="Prepare and return the quarterly report",
                instructions="Create the requested report.",
            ),
            PlanDraft(
                action="plan",
                goal="Prepare and return the quarterly report",
                tasks=(task,),
            ),
            AnswerIntent(
                action="answer",
                kind="final",
                instructions="Summarize the completed report for the user.",
            ),
        ]
    )
    dependencies = _dependencies(agent)
    auto_complete = _AutoCompleteRootOrchestration(
        [("succeeded", {"report": {"text": "Quarterly report complete."}})]
    )
    dependencies.root_orchestration = auto_complete

    result = await invoke_root_graph(
        _build_graph(dependencies),
        session_id="session-1",
        run_id="run-completed-goal",
        message_id="message-completed-goal",
        user_message="Prepare and return the quarterly report.",
    )

    assert result["status"] == "completed"
    assert result["result"]["kind"] == "final"
    assert agent.inputs[-1].completed_plans[0].goal == (
        "Prepare and return the quarterly report"
    )
    completed_task = agent.inputs[-1].completed_plans[0].tasks[0]
    assert completed_task.objective == "Prepare the requested report"
    assert completed_task.acceptance_criteria == "Return the completed report text"
    assert completed_task.output == {"text": "Quarterly report complete."}


@pytest.mark.asyncio
async def test_root_graph_can_replan_a_concrete_goal_after_partial_failure() -> None:
    task = {
        "task_ref": "source",
        "contract_id": "worker.source",
        "contract_revision": 1,
        "objective": "Fetch source material",
        "input": {"source": "approved"},
        "acceptance_criteria": "Return verified source material",
    }
    agent = _Agent(
        [
            PlanIntent(
                action="plan",
                goal="Prepare a report using source material",
                instructions="Fetch the approved source.",
            ),
            PlanDraft(
                action="plan",
                goal="Prepare a report using source material",
                tasks=(task,),
            ),
            PlanIntent(
                action="plan",
                goal="Complete the report using an alternative approved source",
                instructions="Recover the missing source material.",
            ),
            PlanDraft(
                action="plan",
                goal="Complete the report using an alternative approved source",
                tasks=(task,),
            ),
            AnswerIntent(
                action="answer",
                kind="final",
                instructions="Summarize the recovered result.",
            ),
        ]
    )
    dependencies = _dependencies(agent, root_max_turns=8)
    auto_complete = _AutoCompleteRootOrchestration(
        [
            ("failed", {}),
            ("succeeded", {"source": {"text": "Alternative source found."}}),
        ]
    )
    dependencies.root_orchestration = auto_complete

    result = await invoke_root_graph(
        _build_graph(dependencies),
        session_id="session-1",
        run_id="run-partial-failure-replan",
        message_id="message-partial-failure-replan",
        user_message="Prepare a report using approved source material.",
    )

    assert result["status"] == "completed"
    assert len(auto_complete.submissions) == 2
    assert agent.inputs[2].completed_plans[0].tasks[0].status == "failed"
    assert agent.inputs[2].completed_plans[0].tasks[0].error == {
        "code": "source_unavailable"
    }
    assert agent.inputs[2].messages[-1].content == (
        "Prepare a report using approved source material."
    )
    assert agent.inputs[-1].completed_plans[1].tasks[0].output == {
        "text": "Alternative source found."
    }


@pytest.mark.asyncio
async def test_root_graph_turns_stale_planning_feedback_into_clarification() -> None:
    agent = _Agent(
        [
            PlanIntent(
                action="plan",
                goal="Prepare the requested report",
                instructions="Use a Worker for the report.",
            ),
            PlanningFeedback(
                action="needs_information",
                missing_information=("目标报告的收件人",),
                reason="没有足够信息确定报告的目标受众。",
            ),
            PlanIntent(
                action="plan",
                goal="Prepare the requested report",
                instructions="Continue planning if possible.",
            ),
        ]
    )
    result = await invoke_root_graph(
        _build_graph(_dependencies(agent)),
        session_id="session-1",
        run_id="run-planning-feedback-clarification",
        message_id="message-planning-feedback-clarification",
        user_message="Prepare the requested report.",
    )

    assert result["status"] == "completed"
    assert result["result"]["kind"] == "clarification"
    assert "目标报告的收件人" in result["result"]["content"]
    assert "目标受众" in result["result"]["content"]
    assert len(agent.inputs) == 3


@pytest.mark.asyncio
async def test_root_budget_reserves_a_turn_before_context_retrieval() -> None:
    provider_calls = 0

    async def retrieve(*, session_id, run_id, plan):
        nonlocal provider_calls
        provider_calls += 1
        return ProviderResult("memory", "succeeded", result_count=0)

    request_context = ContextRequest(
        action="context",
        queries=(MemoryQuery(provider="memory", query="same query"),),
    )
    agent = _Agent([request_context])
    dependencies = _dependencies(agent, provider=retrieve, root_max_turns=1)

    result = await invoke_root_graph(
        _build_graph(dependencies),
        session_id="session-1",
        run_id="run-budget",
        message_id="message-budget",
        user_message="查找资料",
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "root_budget_exhausted"
    assert provider_calls == 1  # 只执行入口预检索，最后一轮不能发起定向检索。
    assert len(agent.inputs) == 1


@pytest.mark.asyncio
async def test_root_allows_plan_on_last_decision_turn() -> None:
    agent = _Agent(
        [
            PlanIntent(
                action="plan",
                goal="列出目录文件",
                instructions="使用可用工具列出指定目录中的文件名。",
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
            AnswerIntent(
                action="answer",
                kind="final",
                instructions="I cannot access memory now.",
            ),
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
    assert agent.inputs[1].context_queries[-1].status == "skipped"
    assert agent.inputs[1].context_queries[-1].stage == "directed"
    assert agent.inputs[1].context_queries[-1].error_message == "context_provider_unavailable"


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


@pytest.mark.asyncio
async def test_domain_plan_resume_value_is_serialized_for_langgraph_command() -> None:
    class CapturingGraph:
        def __init__(self):
            self.command = None

        async def ainvoke(self, command, *, config):
            self.command = command
            return {"status": "completed"}

    graph = CapturingGraph()
    resume_value = RootPlanCompletedResumeValue(
        type="plan_completed",
        run_id="run-domain-resume",
        plan_id="plan-1",
        plan_sequence=1,
        root_wait_generation=1,
        interrupt_id="interrupt-1",
    )

    await invoke_root_graph(
        graph,
        session_id="session-1",
        run_id="run-domain-resume",
        user_message="",
        resume_value=resume_value,
    )

    assert graph.command.resume == {
        "type": "plan_completed",
        "run_id": "run-domain-resume",
        "plan_id": "plan-1",
        "plan_sequence": 1,
        "root_wait_generation": 1,
        "interrupt_id": "interrupt-1",
    }


@pytest.mark.asyncio
async def test_checkpoint_recovery_after_plan_consumption_uses_new_lease() -> None:
    class Agent(_Agent):
        def __init__(self):
            super().__init__(
                [
                    PlanIntent(action="plan", goal="Collect evidence", instructions="Use the Worker."),
                    PlanDraft(
                        action="plan",
                        goal="Collect evidence",
                        tasks=(
                            {
                                "task_ref": "echo",
                                "contract_id": "echo",
                                "contract_revision": 1,
                                "objective": "Return the supplied value.",
                                "input": {"value": "first-round-output"},
                                "acceptance_criteria": "Return the same value.",
                            },
                        ),
                    ),
                    AnswerIntent(action="answer", kind="final", instructions="Summarize the results."),
                ]
            )
            self.redecision_count = 0

        async def redecide(self, inputs):
            self.inputs.append(inputs)
            self.redecision_count += 1
            if self.redecision_count == 1:
                raise asyncio.CancelledError()
            return self.decisions.pop(0)

    class Checkpointer:
        def __init__(self, saver):
            self.saver = saver

        def get_saver(self):
            return self.saver

    class ThreadLock:
        @asynccontextmanager
        async def hold(self, _key):
            yield

    class LeaseCenter:
        async def assert_current(self, _lease):
            return None

    agent = Agent()
    dependencies = _dependencies(agent)
    saver = InMemorySaver()
    workflow = RootWorkflow(dependencies)
    graph = build_root_graph(
        workflow=workflow,
        dependencies=dependencies,
        checkpointer=saver,
    )
    lease = lease_grant("root_execution", "run-checkpoint-recovery")
    interrupted = await invoke_root_graph(
        graph,
        session_id="session-1",
        run_id="run-checkpoint-recovery",
        message_id="message-1",
        user_message="Collect evidence.",
        lease=lease,
    )
    interrupt = interrupted["__interrupt__"][0]
    wait_identity = interrupt.value
    dependencies.root_orchestration.completed_plan = RootPlanFacts(
        run_id="run-checkpoint-recovery",
        plan_id=wait_identity["plan_id"],
        sequence=wait_identity["plan_sequence"],
        root_wait_generation=wait_identity["root_wait_generation"],
        goal="Collect evidence",
        status="completed",
        tasks=(
            RootPlanTaskFact(
                task_ref="echo",
                status="succeeded",
                output={"value": "first-round-output"},
            ),
        ),
    )
    resume = RootPlanCompletedResumeValue(
        type="plan_completed",
        run_id="run-checkpoint-recovery",
        plan_id=wait_identity["plan_id"],
        plan_sequence=wait_identity["plan_sequence"],
        root_wait_generation=wait_identity["root_wait_generation"],
        interrupt_id=interrupt.id,
    )

    with pytest.raises(BaseException):
        await invoke_root_graph(
            graph,
            session_id="session-1",
            run_id="run-checkpoint-recovery",
            lease=lease,
            resume_value=resume,
        )

    config = {"configurable": {"thread_id": "run:run-checkpoint-recovery"}}
    snapshot = await graph.aget_state(config)
    assert not any(task.interrupts for task in snapshot.tasks)
    assert snapshot.next == ("redecide_root_action",)
    assert len(snapshot.values["completed_plans"]) == 1

    root_graph = LangGraphRootGraph(
        workflow=workflow,
        dependencies=dependencies,
        checkpointer=Checkpointer(saver),
        thread_lock=ThreadLock(),
        lease_center=LeaseCenter(),
    )
    assert await root_graph.is_plan_checkpoint_recoverable(
        run_id="run-checkpoint-recovery",
        plan_id=wait_identity["plan_id"],
        plan_sequence=wait_identity["plan_sequence"],
        root_wait_generation=wait_identity["root_wait_generation"],
    )
    resumed_lease = lease_grant(
        "root_execution", "run-checkpoint-recovery", fencing_token=2
    )
    result = await invoke_root_graph(
        graph,
        session_id="session-1",
        run_id="run-checkpoint-recovery",
        lease=resumed_lease,
        resume_value=RootPlanCheckpointRecovery(
            type="plan_checkpoint_recovery",
            run_id="run-checkpoint-recovery",
            plan_id=wait_identity["plan_id"],
            plan_sequence=wait_identity["plan_sequence"],
            root_wait_generation=wait_identity["root_wait_generation"],
        ),
    )

    assert result["status"] == "completed"
    assert agent.redecision_count == 2
    assert len(dependencies.root_orchestration.submissions) == 1
    assert len(agent.inputs[-1].completed_plans) == 1
    assert agent.inputs[-1].completed_plans[0].tasks[0].output == {
        "value": "first-round-output"
    }
    final_snapshot = await graph.aget_state(config)
    assert final_snapshot.values["execution_lease"]["fencing_token"] == 2
