"""Structured error information collection and LLM prompt generation.

Assembles retry history, circuit state, and tool metadata into a
standardized error report that can be consumed by the LLM decision engine.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from athena.core.resilience.retry import RetryResult


@dataclass
class AttemptInfo:
    """Structured information about a single retry attempt."""

    attempt: int
    timestamp: float
    success: bool
    duration_ms: float
    error_type: str | None = None
    error_message: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AttemptInfo:
        """Create an AttemptInfo from a dictionary."""
        return cls(
            attempt=data["attempt"],
            timestamp=data["timestamp"],
            success=data["success"],
            duration_ms=data["duration_ms"],
            error_type=data.get("error_type"),
            error_message=data.get("error_message"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "attempt": self.attempt,
            "timestamp": self.timestamp,
            "success": self.success,
            "duration_ms": self.duration_ms,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


@dataclass
class StructuredError:
    """Standardized error information passed to the LLM decision engine."""

    error_type: str
    error_message: str
    tool_name: str
    tool_args: dict[str, Any]
    total_attempts: int
    retry_history: list[AttemptInfo]
    circuit_state: str
    circuit_failure_rate: float
    timestamp: float
    server_id: str
    duration_total_ms: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StructuredError:
        """Create a StructuredError from a dictionary."""
        return cls(
            error_type=data["error_type"],
            error_message=data["error_message"],
            tool_name=data["tool_name"],
            tool_args=data["tool_args"],
            total_attempts=data["total_attempts"],
            retry_history=[
                AttemptInfo.from_dict(a) for a in data.get("retry_history", [])
            ],
            circuit_state=data["circuit_state"],
            circuit_failure_rate=data["circuit_failure_rate"],
            timestamp=data["timestamp"],
            server_id=data["server_id"],
            duration_total_ms=data["duration_total_ms"],
        )

    def to_llm_prompt(self) -> str:
        """Render into a human/LLM-readable text block."""
        retry_lines: list[str] = []
        for a in self.retry_history:
            status = "OK" if a.success else "FAIL"
            err = a.error_type or "N/A" if not a.success else "success"
            msg = (a.error_message or "")[:100]
            retry_lines.append(
                f"  Attempt {a.attempt + 1} [{status}] {err}: {msg}"
            )

        args_str = json.dumps(self.tool_args, ensure_ascii=False)
        if len(args_str) > 500:
            args_str = args_str[:500] + "..."

        retry_block = "\n".join(retry_lines) if retry_lines else "  (no attempts)"

        return (
            f"## Tool Call Failure Report\n"
            f"\n"
            f"- **Tool**: {self.tool_name}\n"
            f"- **Server**: {self.server_id}\n"
            f"- **Arguments**: {args_str}\n"
            f"- **Error Type**: {self.error_type}\n"
            f"- **Error Message**: {self.error_message[:300]}\n"
            f"- **Total Attempts**: {self.total_attempts}\n"
            f"- **Total Duration**: {self.duration_total_ms:.0f} ms\n"
            f"- **Circuit State**: {self.circuit_state}"
            f" (failure rate: {self.circuit_failure_rate:.1%})\n"
            f"\n"
            f"### Retry History\n"
            f"{retry_block}\n"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (for JSON logging or API responses)."""
        return {
            "error_type": self.error_type,
            "error_message": self.error_message,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "total_attempts": self.total_attempts,
            "retry_history": [a.to_dict() for a in self.retry_history],
            "circuit_state": self.circuit_state,
            "circuit_failure_rate": self.circuit_failure_rate,
            "timestamp": self.timestamp,
            "server_id": self.server_id,
            "duration_total_ms": self.duration_total_ms,
        }


class ErrorCollector:
    """Assembles ``StructuredError`` from retry results and circuit state."""

    @staticmethod
    def collect(
        tool_name: str,
        tool_args: dict[str, Any],
        server_id: str,
        retry_result: RetryResult,
        circuit_state: str,
        circuit_failure_rate: float,
    ) -> StructuredError:
        last_error = retry_result.error
        error_type = type(last_error).__name__ if last_error else "UnknownError"
        error_message = str(last_error) if last_error else "Unknown error"

        # Prefer the classified type from the last attempt
        if retry_result.attempts:
            last_attempt = retry_result.attempts[-1]
            error_type = last_attempt.error_type or error_type
            error_message = last_attempt.error_message or error_message

        total_duration = sum(a.duration_ms for a in retry_result.attempts)

        return StructuredError(
            error_type=error_type,
            error_message=error_message,
            tool_name=tool_name,
            tool_args=tool_args,
            total_attempts=retry_result.total_attempts,
            retry_history=[
                AttemptInfo(
                    attempt=a.attempt,
                    timestamp=a.timestamp,
                    success=a.success,
                    duration_ms=a.duration_ms,
                    error_type=a.error_type,
                    error_message=a.error_message,
                )
                for a in retry_result.attempts
            ],
            circuit_state=circuit_state,
            circuit_failure_rate=circuit_failure_rate,
            timestamp=time.time(),
            server_id=server_id,
            duration_total_ms=total_duration,
        )
