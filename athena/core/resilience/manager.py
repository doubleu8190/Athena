"""ResilienceManager — aggregates all resilience components.

Provides a single entry point used by ``tools_node`` to execute tool
calls with retry, circuit-breaking, error collection, and optional
LLM-based recovery decisions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from athena.api.metrics import athena_llm_decision_total, athena_tool_call_duration_seconds
from athena.core.resilience.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakerManager,
)
from athena.core.resilience.error_collector import ErrorCollector, StructuredError
from athena.core.resilience.llm_decision import (
    LLMDecision,
    LLMDecisionConfig,
    LLMDecisionEngine,
    LLMProvider,
)
from athena.core.resilience.retry import (
    CircuitOpenError,
    RetryConfig,
    RetryExecutor,
)
from athena.logging_config import get_logger

logger = get_logger(__name__)


class ToolFunc(Protocol):
    """Callable that executes a tool with given arguments."""

    async def __call__(self, args: dict[str, Any]) -> Any: ...


class ToolRegistryProtocol(Protocol):
    """Minimal registry interface needed for fallback resolution."""

    def resolve_fallback(
        self, failed_tool_name: str, capability_tag: str | None = None
    ) -> Any | None: ...

    def get_tool_by_name(
        self, name: str, server_id: str | None = None
    ) -> Any | None: ...


class ResilienceConfig:
    """Top-level configuration container."""

    def __init__(
        self,
        retry: RetryConfig | None = None,
        circuit_breaker: CircuitBreakerConfig | None = None,
        llm_decision: LLMDecisionConfig | None = None,
    ) -> None:
        self.retry = retry or RetryConfig()
        self.circuit_breaker = circuit_breaker or CircuitBreakerConfig()
        self.llm_decision = llm_decision or LLMDecisionConfig()


class ResilienceManager:
    """Coordinates retry, circuit breaker, error collection, and LLM decisions.

    Used as an optional dependency injected into ``tools_node`` via
    ``RunnableConfig.configurable``.
    """

    def __init__(
        self,
        config: ResilienceConfig,
        llm_provider: LLMProvider | None = None,
        tool_registry: ToolRegistryProtocol | None = None,
    ) -> None:
        self.config = config
        self.retry_executor = RetryExecutor(config.retry)
        self.cb_manager = CircuitBreakerManager(config.circuit_breaker)
        self.error_collector = ErrorCollector()
        self.tool_registry = tool_registry

        self.llm_engine: LLMDecisionEngine | None = None
        if llm_provider is not None:
            self.llm_engine = LLMDecisionEngine(
                config.llm_decision, llm_provider
            )

    async def execute_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str,
        tool_func: ToolFunc,
        server_id: str = "",
    ) -> ToolExecutionResult:
        """Execute a tool with full resilience handling.

        Flow:
            1. Circuit-breaker fast-fail check
            2. Retry execution with exponential backoff
            3. On failure → collect structured error
            4. LLM decision (if enabled) → execute decision
            5. Return final result or structured error
        """
        import time as _time
        _call_start = _time.monotonic()
        cb = self.cb_manager.get_breaker(tool_name)

        def _observe_duration(success: bool) -> None:
            elapsed = _time.monotonic() - _call_start
            athena_tool_call_duration_seconds.labels(
                tool_name=tool_name, success=str(success),
            ).observe(elapsed)

        # ── Step 1: Circuit breaker gate ────────────────────────────
        if not await cb.allow_request():
            logger.warning(
                "tool_circuit_open",
                tool=tool_name,
                circuit_state=cb.state.value,
            )
            _observe_duration(False)
            return ToolExecutionResult(
                success=False,
                error_message=f"Tool '{tool_name}' is circuit-broken ({cb.state.value}). Please retry later.",
                structured_error=None,
                decision_used=None,
            )

        # ── Step 2: Retry execution ────────────────────────────────
        try:
            retry_result = await self.retry_executor.execute(
                tool_func,
                tool_args,
                tool_name=tool_name,
                circuit_breaker=cb,
            )
        except CircuitOpenError as exc:
            _observe_duration(False)
            return ToolExecutionResult(
                success=False,
                error_message=str(exc),
                structured_error=None,
                decision_used=None,
            )

        if retry_result.success:
            _observe_duration(True)
            return ToolExecutionResult(
                success=True,
                result=retry_result.result,
                structured_error=None,
                decision_used=None,
            )

        # ── Step 3: Collect structured error ───────────────────────
        metrics = cb.get_metrics()
        structured = self.error_collector.collect(
            tool_name=tool_name,
            tool_args=tool_args,
            server_id=server_id,
            retry_result=retry_result,
            circuit_state=metrics["state"],
            circuit_failure_rate=metrics["failure_rate"],
        )

        logger.warning(
            "tool_all_retries_exhausted",
            tool=tool_name,
            total_attempts=structured.total_attempts,
            error_type=structured.error_type,
            circuit_state=structured.circuit_state,
        )

        # ── Step 4: LLM decision ──────────────────────────────────
        if self.llm_engine:
            try:
                decision = await self.llm_engine.decide(structured, tool_call_id)
                if decision is not None:
                    exec_result = await self._execute_decision(
                        decision=decision,
                        original_tool_name=tool_name,
                        original_args=tool_args,
                        tool_func=tool_func,
                        tool_call_id=tool_call_id,
                        server_id=server_id,
                        cb=cb,
                    )
                    if exec_result is not None:
                        _observe_duration(exec_result.success)
                        return exec_result
            finally:
                # Always clean up _decision_count to prevent memory leak,
                # even when _execute_decision returns early (non-abort paths).
                self.llm_engine.cleanup(tool_call_id)

        # ── Step 5: Return structured error ────────────────────────
        _observe_duration(False)
        return ToolExecutionResult(
            success=False,
            error_message=structured.error_message,
            structured_error=structured,
            decision_used=None,
        )

    # ── Decision execution ─────────────────────────────────────────

    async def _execute_decision(
        self,
        decision: LLMDecision,
        original_tool_name: str,
        original_args: dict[str, Any],
        tool_func: ToolFunc,
        tool_call_id: str,
        server_id: str,
        cb: Any,
    ) -> ToolExecutionResult | None:
        """Execute the LLM's recommended decision.

        Returns ``None`` when the decision cannot be fulfilled (e.g.
        fallback tool not found), signalling the caller to fall through
        to the default error path.
        """
        logger.info(
            "llm_decision_received",
            tool=original_tool_name,
            decision=decision.decision,
            confidence=decision.confidence,
            reasoning=decision.reasoning[:200],
        )

        athena_llm_decision_total.labels(
            tool_name=original_tool_name, decision_type=decision.decision,
        ).inc()

        if decision.decision == "retry_with_adjustment":
            return await self._decision_retry(
                decision, original_tool_name, original_args,
                tool_func, tool_call_id, cb,
            )

        if decision.decision == "fallback_tool":
            return await self._decision_fallback(
                decision, original_tool_name, original_args,
                tool_call_id, server_id,
            )

        if decision.decision == "user_intervention":
            return ToolExecutionResult(
                success=False,
                error_message=decision.user_message or "User intervention required.",
                structured_error=None,
                decision_used=decision,
                requires_user_intervention=True,
            )

        if decision.decision == "abort":
            logger.info(
                "llm_decision_abort",
                tool=original_tool_name,
                reason=decision.reason,
            )
            return None

        return None

    async def _decision_retry(
        self,
        decision: LLMDecision,
        tool_name: str,
        original_args: dict[str, Any],
        tool_func: ToolFunc,
        tool_call_id: str,
        cb: Any,
    ) -> ToolExecutionResult:
        adjusted = decision.adjusted_args or original_args
        logger.info(
            "decision_retry_with_adjustment",
            tool=tool_name,
            adjusted_args=adjusted,
        )

        retry_result = await self.retry_executor.execute(
            tool_func,
            adjusted,
            tool_name=tool_name,
            circuit_breaker=cb,
        )

        if retry_result.success:
            return ToolExecutionResult(
                success=True,
                result=retry_result.result,
                structured_error=None,
                decision_used=decision,
            )

        return ToolExecutionResult(
            success=False,
            error_message="Retry with adjusted args also failed.",
            structured_error=None,
            decision_used=decision,
        )

    async def _decision_fallback(
        self,
        decision: LLMDecision,
        original_tool_name: str,
        original_args: dict[str, Any],
        tool_call_id: str,
        server_id: str,
    ) -> ToolExecutionResult | None:
        fallback_name = decision.fallback_tool_name
        if not fallback_name and self.tool_registry:
            fb = self.tool_registry.resolve_fallback(original_tool_name)
            fallback_name = fb.name if fb else None

        if not fallback_name:
            logger.warning(
                "decision_fallback_not_found", tool=original_tool_name
            )
            return None

        logger.info(
            "decision_fallback_tool",
            from_tool=original_tool_name,
            to_tool=fallback_name,
        )

        # We cannot directly call the fallback tool here because we don't
        # have its execution function. Return a result that signals the
        # caller (tools_node) to re-route to the fallback tool.
        return ToolExecutionResult(
            success=False,
            error_message=f"Switching to fallback tool: {fallback_name}",
            structured_error=None,
            decision_used=decision,
            fallback_tool_name=fallback_name,
        )


@dataclass
class ToolExecutionResult:
    """Final result from ``ResilienceManager.execute_tool()``."""

    success: bool
    result: Any = None
    error_message: str = ""
    structured_error: StructuredError | None = None
    decision_used: LLMDecision | None = None
    requires_user_intervention: bool = False
    fallback_tool_name: str | None = None

    def to_message_content(self) -> str:
        """Serialize for a LangChain ToolMessage."""
        if self.success:
            if self.result is None:
                return ""
            if isinstance(self.result, str):
                return self.result
            if isinstance(self.result, (dict, list)):
                return json.dumps(self.result, ensure_ascii=False, indent=2)
            return str(self.result)

        # Error content — include structured info if available
        if self.structured_error:
            return json.dumps(
                self.structured_error.to_dict(),
                ensure_ascii=False,
                indent=2,
            )
        return json.dumps({"error": self.error_message}, ensure_ascii=False)
