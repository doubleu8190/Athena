"""Generic target adapters for Chat and Agent evaluation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from domain.evaluation import EvalCase, EvaluationConfig, ExecutionArtifact


class CallableEvaluationTarget:
    """Adapt an async application callback to the evaluation target port."""

    def __init__(self, handler: Callable[[EvalCase, EvaluationConfig], Awaitable[ExecutionArtifact]]) -> None:
        self.handler = handler

    async def execute(self, case: EvalCase, config: EvaluationConfig) -> ExecutionArtifact:
        return await self.handler(case, config)


class ChatEvaluationTarget(CallableEvaluationTarget):
    """Target marker for isolated chat/session execution."""


class AgentEvaluationTarget(CallableEvaluationTarget):
    """Target marker for sandboxed Agent/tool execution."""


class FixtureChatTarget:
    """Deterministic chat/agent target used by smoke and contract tests.

    A production target can replace this adapter with a session or sandbox
    callback; keeping the fixture target here makes non-LLM CI reproducible.
    """

    async def execute(self, case: EvalCase, config: EvaluationConfig) -> ExecutionArtifact:
        metadata = dict(case.metadata)
        trace = dict(metadata.get("trace") or {})
        return ExecutionArtifact(
            output_text=str(metadata.get("output", "")),
            ranking=tuple(str(item) for item in metadata.get("ranking", ())),
            trace=trace,
            duration_ms=0.0,
        )


__all__ = ["AgentEvaluationTarget", "CallableEvaluationTarget", "ChatEvaluationTarget", "FixtureChatTarget"]
