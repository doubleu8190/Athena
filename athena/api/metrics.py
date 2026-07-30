"""Prometheus metrics setup.

Exposes custom Athena metrics and HTTP request metrics at /metrics.
Uses prometheus_client directly (no third-party instrumentator dependency).
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ── HTTP request metrics ────────────────────────────────────────────────

athena_http_requests_total = Counter(
    "athena_http_requests_total",
    "Total HTTP requests processed",
    ["method", "endpoint", "status_code"],
)

athena_http_request_duration_seconds = Histogram(
    "athena_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30],
)

# ── Custom metrics ────────────────────────────────────────────────────

athena_tasks_total = Counter(
    "athena_tasks_total",
    "Total number of tasks executed",
    ["status"],
)

athena_subtask_duration_seconds = Histogram(
    "athena_subtask_duration_seconds",
    "Subtask execution duration in seconds",
    ["tool_name"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60, 120],
)

athena_fallback_total = Counter(
    "athena_fallback_total",
    "Total number of fallback tool invocations",
    ["from_tool", "to_tool"],
)

athena_circuit_breaker_total = Counter(
    "athena_circuit_breaker_total",
    "Total number of circuit breaker triggers",
)

# ── Tool resilience metrics ──────────────────────────────────────────

athena_tool_retry_total = Counter(
    "athena_tool_retry_total",
    "Total tool retry attempts",
    ["tool_name", "error_type"],
)

athena_tool_retry_success_total = Counter(
    "athena_tool_retry_success_total",
    "Successful tool calls after retry",
    ["tool_name"],
)

athena_tool_circuit_state = Gauge(
    "athena_tool_circuit_state",
    "Tool circuit breaker state (0=closed, 1=open, 2=half_open)",
    ["tool_name"],
)

athena_tool_circuit_trip_total = Counter(
    "athena_tool_circuit_trip_total",
    "Total circuit breaker trip events per tool",
    ["tool_name"],
)

athena_tool_call_duration_seconds = Histogram(
    "athena_tool_call_duration_seconds",
    "Tool call total duration including retries",
    ["tool_name", "success"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60, 120],
)

athena_llm_decision_total = Counter(
    "athena_llm_decision_total",
    "Total LLM recovery decisions",
    ["tool_name", "decision_type"],
)

athena_harness_blocks_total = Counter(
    "athena_harness_blocks_total",
    "Total number of harness rule blocks",
    ["rule_type"],
)

athena_mcp_server_status = Gauge(
    "athena_mcp_server_status",
    "MCP server connection status (1=connected, 0=disconnected)",
    ["server_id"],
)

athena_scheduler_pending_tasks = Gauge(
    "athena_scheduler_pending_tasks",
    "Number of pending/running tasks in the asyncio TaskScheduler",
)
