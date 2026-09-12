from __future__ import annotations

import pytest

from athena.runtime.processors import (
    ProcessAction,
    ProcessDecision,
    ProcessOutcome,
    ProcessorBlocked,
    StaticProcessorProxy,
)


@pytest.mark.asyncio
async def test_proxy_skips_without_running_lifecycle_hooks():
    calls: list[str] = []

    class Processor:
        async def inspect(self, _context):
            calls.append("inspect")
            return ProcessDecision(ProcessAction.SKIP, result="restored")

        async def pre_process(self, _context, _decision):
            calls.append("pre")

        async def process(self, _context, _decision):
            calls.append("process")
            return "new"

        async def post_process(self, _context, _outcome):
            calls.append("post")

    assert await StaticProcessorProxy(Processor()).execute(None) == "restored"
    assert calls == ["inspect"]


@pytest.mark.asyncio
async def test_proxy_calls_post_process_for_failures():
    calls: list[str] = []

    class Processor:
        async def inspect(self, _context):
            return ProcessDecision(ProcessAction.EXECUTE)

        async def pre_process(self, _context, _decision):
            calls.append("pre")

        async def process(self, _context, _decision):
            calls.append("process")
            raise ValueError("boom")

        async def post_process(self, _context, outcome: ProcessOutcome):
            calls.append(outcome.status)

    with pytest.raises(ValueError, match="boom"):
        await StaticProcessorProxy(Processor()).execute(None)
    assert calls == ["pre", "process", "failed"]


@pytest.mark.asyncio
async def test_proxy_blocks_when_recovery_is_ambiguous():
    class Processor:
        async def inspect(self, _context):
            return ProcessDecision(ProcessAction.BLOCK, reason="unknown side effect")

        async def pre_process(self, _context, _decision):
            raise AssertionError("blocked processor must not start")

        async def process(self, _context, _decision):
            raise AssertionError("blocked processor must not execute")

        async def post_process(self, _context, _outcome):
            raise AssertionError("blocked processor has no lifecycle")

    with pytest.raises(ProcessorBlocked, match="unknown side effect"):
        await StaticProcessorProxy(Processor()).execute(None)
