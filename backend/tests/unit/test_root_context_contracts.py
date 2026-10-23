from __future__ import annotations

import asyncio

import pytest

from application.runs.context import (
    ContextAcquisitionService,
    ContextPlan,
    FileQuery,
    KnowledgeQuery,
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
