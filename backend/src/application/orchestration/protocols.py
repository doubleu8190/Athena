"""Application-owned contracts between orchestration use cases and adapters.

The LangGraph adapter implements the graph ports; bootstrap supplies the
workflow and persistence capabilities used by the application.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, AsyncContextManager, Protocol

from domain.orchestration import (
    CommandReceipt,
    OrchestrationCommand,
    Plan,
    ReviewContext,
    ReviewDecision,
    Task,
    TaskExecution,
    TaskResult,
)
from domain.runs import RoundCompletedResumeValue

if TYPE_CHECKING:
    from application.orchestration.langgraph.graphs.state import RootGraphState


class RootAgent(Protocol):
    """Root 工作流所需的 Agent 能力。"""

    async def __call__(
        self, *, harness_input: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    async def review(
        self, context: ReviewContext, *, request: Mapping[str, Any]
    ) -> ReviewDecision: ...


class RootGraphPort(Protocol):
    """Application execution boundary implemented by the LangGraph adapter."""

    async def invoke(
        self,
        *,
        session_id: str,
        run_id: str,
        user_message: str,
        message_id: str = "",
        attachment_ids: list[str] | None = None,
        resume_value: RoundCompletedResumeValue | None = None,
        stop_signal: asyncio.Event | None = None,
    ) -> object: ...

    async def has_pending_round_wait(
        self,
        *,
        run_id: str,
        plan_id: str,
        round_id: str,
        plan_version: int,
        interrupt_id: str,
        checkpoint_id: str,
    ) -> bool: ...

    async def find_pending_round_wait(
        self,
        *,
        run_id: str,
        plan_id: str,
        round_id: str,
        plan_version: int,
    ) -> tuple[str, str] | None: ...


class WorkerExecutor(Protocol):
    """Application capability used by the durable Worker job dispatcher."""

    async def execute(self, execution: TaskExecution) -> TaskResult: ...

    async def resume(
        self,
        execution: TaskExecution,
        decisions: dict[str, str],
    ) -> TaskResult: ...

    async def pending_interrupt(
        self, execution: TaskExecution
    ) -> dict[str, Any] | None: ...


class GraphThreadLock(Protocol):
    """Serialize graph calls for one checkpoint thread."""

    def hold(self, thread_id: str) -> AsyncContextManager[None]: ...


class PlanLoader(Protocol):
    """Load persisted plan status and results for the Root workflow."""

    async def __call__(
        self, *, plan_id: str | None
    ) -> Mapping[str, Any]: ...


class RootPlanCommandPort(Protocol):
    """Application commands required by Root plan submission and review."""

    async def submit_plan(
        self,
        command: OrchestrationCommand,
        plan: Plan,
        tasks: list[Task],
        *,
        legal_tools: Iterable[str] | None = None,
    ) -> CommandReceipt: ...

    async def apply_review_decision(
        self, command: OrchestrationCommand, decision: ReviewDecision
    ) -> CommandReceipt: ...

    async def load_review_decision(
        self,
        *,
        scope_key: str,
        idempotency_key: str,
        plan_id: str,
        plan_version: int,
        round_id: str,
    ) -> ReviewDecision | None: ...


class RootWorkflowPort(Protocol):
    async def prepare_request(self, state: RootGraphState) -> dict[str, Any]: ...
    async def understand_task(self, state: RootGraphState) -> dict[str, Any]: ...
    async def clarification_response(
        self, state: RootGraphState
    ) -> dict[str, Any]: ...
    async def plan_context(self, state: RootGraphState) -> dict[str, Any]: ...
    async def acquire_context(self, state: RootGraphState) -> dict[str, Any]: ...
    async def prepare_harness_input(
        self, state: RootGraphState
    ) -> dict[str, Any]: ...
    async def run_root_execution_loop(
        self, state: RootGraphState
    ) -> dict[str, Any]: ...
    async def materialize_execution_plan(
        self, state: RootGraphState
    ) -> dict[str, Any]: ...
    async def wait_for_plan_completion(
        self, state: RootGraphState
    ) -> dict[str, Any]: ...
    async def review_completed_round(
        self, state: RootGraphState
    ) -> dict[str, Any]: ...
    async def build_plan_response(self, state: RootGraphState) -> dict[str, Any]: ...
    async def assemble_final_response(
        self, state: RootGraphState
    ) -> dict[str, Any]: ...


class WorkerWorkflowPort(Protocol):
    async def prepare(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def approval_gate(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def execute_tool_batch(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    async def complete_tool_approval(
        self, state: Mapping[str, Any], decision: Any
    ) -> dict[str, Any]: ...
    async def finalize(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    def route_after_gate(self, state: Mapping[str, Any]) -> str: ...
    def route_after_tools(self, state: Mapping[str, Any]) -> str: ...


__all__ = [
    "RootAgent",
    "PlanLoader",
    "RootGraphPort",
    "WorkerExecutor",
    "GraphThreadLock",
    "RootPlanCommandPort",
    "RootWorkflowPort",
    "WorkerWorkflowPort",
]
