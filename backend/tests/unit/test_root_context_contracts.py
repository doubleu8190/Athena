from __future__ import annotations

import asyncio

import pytest

from application.runs.context import (
    ContextAcquisitionService,
    ContextItem,
    ContextPlan,
    FileQuery,
    KnowledgeQuery,
    ProviderResult,
)
from application.runs.context.planner import ContextPlanner, InvalidContextResource


def test_context_planner_rejects_file_ids_outside_current_request() -> None:
    planner = ContextPlanner()

    with pytest.raises(InvalidContextResource, match="context_file_outside_current_request"):
        planner.plan(
            (FileQuery(provider="file", query="find report", file_ids=("other",)),),
            attachment_ids=("current",),
            knowledge_base_ids=(),
        )


def test_context_planner_rejects_unavailable_knowledge_base_ids() -> None:
    planner = ContextPlanner()

    with pytest.raises(InvalidContextResource, match="context_knowledge_base_unavailable"):
        planner.plan(
            (
                KnowledgeQuery(
                    provider="knowledge",
                    query="find policy",
                    knowledge_base_ids=("missing",),
                ),
            ),
            attachment_ids=(),
            knowledge_base_ids=("available",),
        )


@pytest.mark.asyncio
async def test_cancelled_retrieval_cancels_provider_and_publishes_failure() -> None:
    provider_started = asyncio.Event()
    provider_cancelled = asyncio.Event()

    class Provider:
        async def retrieve_context(self, *, session_id, run_id, plan):
            provider_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                provider_cancelled.set()

    class TraceRepository:
        async def record_context(self, **_kwargs):
            return None

    class EventPublisher:
        def __init__(self):
            self.events = []

        async def publish(self, event):
            self.events.append(event)

    events = EventPublisher()
    service = ContextAcquisitionService(
        {"memory": Provider()},
        trace_repository=TraceRepository(),
        event_publisher=events,
    )
    retrieval = asyncio.create_task(
        service.retrieve_context(
            session_id="session-1",
            run_id="run-1",
            invocation_id="call-1",
            stage="prefetch",
            plan=ContextPlan(providers=("memory",)),
        )
    )
    await asyncio.wait_for(provider_started.wait(), timeout=1)

    retrieval.cancel()
    with pytest.raises(asyncio.CancelledError):
        await retrieval

    assert provider_cancelled.is_set()
    failed_events = [event for event in events.events if event.event_type == "retrieval.failed"]
    assert len(failed_events) == 1
    assert failed_events[0].payload["status"] == "cancelled"


@pytest.mark.asyncio
async def test_prefetch_deadline_keeps_fast_provider_and_records_stage() -> None:
    """完整通道超时保留另一通道，并将阶段写入事件与轨迹；无参数。"""
    cancelled = asyncio.Event()
    started = asyncio.Event()
    recorded = []
    events = []

    class SlowKnowledge:
        async def retrieve_context(self, **kwargs):
            """模拟枚举知识库文档时阻塞，结束时记录清理。"""
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    class FastMemory:
        async def retrieve_context(self, **kwargs):
            """等待慢通道已开始后返回，证明两通道同时运行。"""
            await started.wait()
            return ProviderResult("memory", "succeeded", (ContextItem("memory", "苹果", "fruit"),), result_count=1)

    class Trace:
        async def record_context(self, **kwargs):
            """保留原始写入参数供断言。"""
            recorded.append(kwargs)
            return "retrieval-deadline"

    class Events:
        async def publish(self, event):
            """保留本批完整事件。"""
            events.append(event)

    service = ContextAcquisitionService({"memory": FastMemory(), "knowledge": SlowKnowledge()}, Trace(), Events())
    bundle = await asyncio.wait_for(service.retrieve_context(
        session_id="s", run_id="r", invocation_id="deadline", stage="prefetch",
        plan=ContextPlan(providers=("memory", "knowledge"), memory_query="水果", knowledge_query="水果", provider_timeout_seconds=0.02),
    ), timeout=1)
    assert cancelled.is_set()
    assert bundle.items[0].content == "苹果"
    assert {item.provider: item.status for item in bundle.provider_results} == {"memory": "succeeded", "knowledge": "timeout"}
    assert recorded[0]["status"] == "partial"
    assert all(item.effective_params["retrieval_stage"] == "prefetch" for item in recorded[0]["providers"])
    assert all(event.payload["stage"] == "prefetch" for event in events)
    assert events[0].payload["name"] == "prefetch_context"


@pytest.mark.asyncio
async def test_directed_lookup_uses_existing_provider_timeout_without_prefetch_deadline() -> None:
    """定向计划未设置预检索上限时可正常完成；无参数。"""
    class Provider:
        async def retrieve_context(self, **kwargs):
            """返回空结果，由工作流决定是否澄清。"""
            assert kwargs["plan"].provider_timeout_seconds is None
            return ProviderResult("memory", "succeeded", result_count=0)

    class Trace:
        async def record_context(self, **kwargs):
            """验证定向阶段写入既有 JSON 扩展信息。"""
            assert kwargs["providers"][0].effective_params["retrieval_stage"] == "directed"
            return "retrieval-directed"

    class Events:
        async def publish(self, event):
            """验证定向活动名称与阶段。"""
            if event.event_type == "retrieval.started":
                assert event.payload["name"] == "acquire_context"
                assert event.payload["stage"] == "directed"

    bundle = await ContextAcquisitionService({"memory": Provider()}, Trace(), Events()).retrieve_context(
        session_id="s", run_id="r", invocation_id="directed", stage="directed",
        plan=ContextPlan(providers=("memory",), memory_query="水果"),
    )
    assert bundle.provider_results[0].status == "succeeded"
