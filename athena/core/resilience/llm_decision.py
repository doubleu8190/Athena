"""LLM-based decision engine for tool failure recovery.

Sends a structured error report to the LLM and parses a JSON decision
indicating the next action: retry with adjusted params, fallback tool,
user intervention, or abort.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from athena.core.resilience.error_collector import StructuredError
from athena.logging_config import get_logger

logger = get_logger(__name__)

# ── Prompt template ─────────────────────────────────────────────────

DECISION_PROMPT_TEMPLATE = """\
You are a system operations assistant. A tool call has failed after \
multiple retries. Analyze the failure and recommend the next action.

{error_report}

## Available Decision Types

1. **retry_with_adjustment** — Retry with modified arguments.
   - Use when: arguments may be incorrect or a temporary tweak could help.
   - MUST include `adjusted_args` field.

2. **fallback_tool** — Switch to an alternative tool.
   - Use when: the current tool is persistently unavailable.
   - MUST include `fallback_tool_name` field.

3. **user_intervention** — Ask the user for help.
   - Use when: permission is missing, configuration is wrong, \
or human confirmation is needed.
   - MUST include `user_message` field (clear, non-technical).

4. **abort** — Give up.
   - Use when: the error is unrecoverable or risk is too high.
   - MUST include `reason` field.

Respond with a single JSON object:
```json
{{
  "decision": "<one of the four types above>",
  "confidence": 0.0,
  "reasoning": "your analysis",
  ... additional fields per decision type
}}
```"""


@dataclass
class LLMDecisionConfig:
    """Configuration for the LLM decision engine."""

    enabled: bool = True
    max_decisions: int = 3
    decision_timeout_seconds: int = 30
    allowed_decisions: list[str] = field(default_factory=lambda: [
        "retry_with_adjustment",
        "fallback_tool",
        "user_intervention",
        "abort",
    ])

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LLMDecisionConfig:
        """Create an LLMDecisionConfig from a dictionary."""
        default_allowed = cls().allowed_decisions
        return cls(
            enabled=data.get("enabled", True),
            max_decisions=data.get("max_decisions", 3),
            decision_timeout_seconds=data.get("decision_timeout_seconds", 30),
            allowed_decisions=data.get("allowed_decisions", default_allowed),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "enabled": self.enabled,
            "max_decisions": self.max_decisions,
            "decision_timeout_seconds": self.decision_timeout_seconds,
            "allowed_decisions": self.allowed_decisions,
        }


@dataclass
class LLMDecision:
    """Parsed decision from the LLM."""

    decision: str
    confidence: float = 0.5
    reasoning: str = ""
    adjusted_args: dict[str, Any] | None = None
    fallback_tool_name: str | None = None
    user_message: str | None = None
    reason: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LLMDecision:
        """Create an LLMDecision from a dictionary (e.g., parsed JSON)."""
        return cls(
            decision=data.get("decision", "abort"),
            confidence=float(data.get("confidence", 0.5)),
            reasoning=data.get("reasoning") or "",
            adjusted_args=data.get("adjusted_args"),
            fallback_tool_name=data.get("fallback_tool_name"),
            user_message=data.get("user_message"),
            reason=data.get("reason"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        result: dict[str, Any] = {
            "decision": self.decision,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
        }
        if self.adjusted_args is not None:
            result["adjusted_args"] = self.adjusted_args
        if self.fallback_tool_name is not None:
            result["fallback_tool_name"] = self.fallback_tool_name
        if self.user_message is not None:
            result["user_message"] = self.user_message
        if self.reason is not None:
            result["reason"] = self.reason
        return result


class LLMProvider(Protocol):
    """Minimal interface the decision engine needs from an LLM."""

    async def ainvoke(self, prompt: str) -> str: ...


class LLMDecisionEngine:
    """Sends error reports to an LLM and parses structured decisions."""

    def __init__(
        self, config: LLMDecisionConfig, llm_provider: LLMProvider
    ) -> None:
        self.config = config
        self.llm = llm_provider
        self._decision_count: dict[str, int] = {}

    async def decide(
        self,
        structured_error: StructuredError,
        tool_call_id: str,
    ) -> LLMDecision | None:
        """Request an LLM decision for the given error.

        Returns ``None`` when the engine is disabled, the decision
        limit has been reached, or the LLM call fails.
        """
        if not self.config.enabled:
            return None

        count = self._decision_count.get(tool_call_id, 0)
        if count >= self.config.max_decisions:
            logger.warning(
                "llm_decision_limit_reached",
                tool_call_id=tool_call_id,
                count=count,
            )
            return None

        self._decision_count[tool_call_id] = count + 1

        prompt = DECISION_PROMPT_TEMPLATE.format(
            error_report=structured_error.to_llm_prompt()
        )

        try:
            response = await asyncio.wait_for(
                self.llm.ainvoke(prompt),
                timeout=self.config.decision_timeout_seconds,
            )
            return self._parse_decision(response)

        except TimeoutError:
            logger.error("llm_decision_timeout", tool_call_id=tool_call_id)
            return LLMDecision(
                decision="abort",
                confidence=1.0,
                reasoning="LLM decision timed out",
                reason="Decision timeout",
            )
        except Exception as exc:
            logger.error(
                "llm_decision_error",
                tool_call_id=tool_call_id,
                error=str(exc),
            )
            return None

    def cleanup(self, tool_call_id: str) -> None:
        """Remove tracking state for a completed tool call."""
        self._decision_count.pop(tool_call_id, None)

    # ── Internal ───────────────────────────────────────────────────

    def _parse_decision(self, response: str) -> LLMDecision:
        """Extract a JSON decision object from the LLM response."""
        try:
            json_str = self._extract_json(response)
            if json_str is None:
                raise ValueError("No JSON found in LLM response")

            data = json.loads(json_str)
            decision_obj = LLMDecision.from_dict(data)

            if decision_obj.decision not in self.config.allowed_decisions:
                logger.warning(
                    "llm_invalid_decision_type", decision=decision_obj.decision
                )
                decision_obj.decision = "abort"

            return decision_obj

        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("llm_decision_parse_failed", error=str(exc))
            return LLMDecision(
                decision="abort",
                confidence=1.0,
                reasoning="Failed to parse LLM decision",
                reason=f"Parse error: {exc}",
            )

    @staticmethod
    def _extract_json(text: str) -> str | None:
        """Extract a JSON object from LLM output, handling markdown fences and surrounding text.

        Strategy:
        1. Try to find a ```json ... ``` block and extract the content.
        2. Fall back to balanced-brace matching from the first '{'.
        """
        # 1. Try markdown fenced code block
        fence_match = re.search(r"```(?:json)?\s*\n?(\{.*?\})\s*\n?```", text, re.DOTALL)
        if fence_match:
            return fence_match.group(1).strip()

        # 2. Balanced-brace extraction from the first '{'
        start = text.find("{")
        if start == -1:
            return None

        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]

        return None
