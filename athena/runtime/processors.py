"""Reusable lifecycle processors for side-effecting runtime operations.

LangGraph checkpoints cover node-to-node state, while these processors cover
the smaller operation inside a node that may have already changed durable
state before the process stopped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

ResultT = TypeVar("ResultT")
ContextT = TypeVar("ContextT", contravariant=True)


class ProcessAction(StrEnum):
    EXECUTE = "execute"
    RESUME = "resume"
    SKIP = "skip"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class ProcessDecision(Generic[ResultT]):
    """Decision returned by a processor's read-only inspection phase."""

    action: ProcessAction
    result: ResultT | None = None
    resume_from: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessOutcome(Generic[ResultT]):
    """Terminal outcome passed to ``post_process``."""

    status: str
    result: ResultT | None = None
    error: BaseException | None = None


class ProcessorBlocked(RuntimeError):
    """Raised when recovery cannot safely decide whether to retry an operation."""


class Processor(Protocol[ContextT, ResultT]):
    """An asynchronous, inspectable operation with lifecycle hooks."""

    async def inspect(self, context: ContextT) -> ProcessDecision[ResultT]:
        """Read durable state and choose execute/resume/skip/block."""
        ...

    async def pre_process(
        self, context: ContextT, decision: ProcessDecision[ResultT]
    ) -> None:
        """Prepare durable state and publish a start event."""
        ...

    async def process(
        self, context: ContextT, decision: ProcessDecision[ResultT]
    ) -> ResultT:
        """Perform the business operation."""
        ...

    async def post_process(
        self, context: ContextT, outcome: ProcessOutcome[ResultT]
    ) -> None:
        """Publish terminal events and finish durable cleanup."""
        ...


class StaticProcessorProxy(Generic[ContextT, ResultT]):
    """Run a processor with a fixed, exception-safe lifecycle."""

    def __init__(self, processor: Processor[ContextT, ResultT]) -> None:
        self._processor = processor

    async def execute(self, context: ContextT) -> ResultT:
        decision = await self._processor.inspect(context)
        if decision.action is ProcessAction.SKIP:
            if decision.result is None:
                raise ProcessorBlocked(
                    decision.reason or "processor skipped without a recoverable result"
                )
            return decision.result
        if decision.action is ProcessAction.BLOCK:
            raise ProcessorBlocked(decision.reason or "processor recovery is blocked")

        outcome: ProcessOutcome[ResultT] = ProcessOutcome(status="failed")
        try:
            await self._processor.pre_process(context, decision)
            result = await self._processor.process(context, decision)
            outcome = ProcessOutcome(status="completed", result=result)
            return result
        except asyncio.CancelledError as exc:
            outcome = ProcessOutcome(status="cancelled", error=exc)
            raise
        except BaseException as exc:
            outcome = ProcessOutcome(status="failed", error=exc)
            raise
        finally:
            try:
                await self._processor.post_process(context, outcome)
            except BaseException as post_error:
                # Cleanup/terminal-event failures must not hide the business
                # exception that caused a failed or cancelled operation.
                if outcome.error is not None:
                    raise outcome.error from post_error
                raise


async def run_with_lifecycle(
    process: Callable[[], Awaitable[ResultT]],
    *,
    inspect: Callable[[], Awaitable[ProcessDecision[ResultT]]] | None = None,
    pre_process: Callable[[ProcessDecision[ResultT]], Awaitable[None]] | None = None,
    post_process: Callable[[ProcessOutcome[ResultT]], Awaitable[None]] | None = None,
) -> ResultT:
    """Adapt plain callables to the processor contract."""

    class _CallableProcessor:
        async def inspect(self, context: None) -> ProcessDecision[ResultT]:
            return (
                await inspect()
                if inspect is not None
                else ProcessDecision(ProcessAction.EXECUTE)
            )

        async def pre_process(
            self, context: None, decision: ProcessDecision[ResultT]
        ) -> None:
            if pre_process is not None:
                await pre_process(decision)

        async def process(
            self, context: None, decision: ProcessDecision[ResultT]
        ) -> ResultT:
            return await process()

        async def post_process(
            self, context: None, outcome: ProcessOutcome[ResultT]
        ) -> None:
            if post_process is not None:
                await post_process(outcome)

    return await StaticProcessorProxy(_CallableProcessor()).execute(None)


__all__ = [
    "ProcessAction",
    "ProcessDecision",
    "ProcessOutcome",
    "Processor",
    "ProcessorBlocked",
    "StaticProcessorProxy",
    "run_with_lifecycle",
]
