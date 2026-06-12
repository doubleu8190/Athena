"""Prometheus metrics setup.

Uses prometheus_fastapi_instrumentator to expose metrics at /metrics.
Additional custom metrics for Athena-specific observability.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

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

athena_celery_queue_depth = Gauge(
    "athena_celery_queue_depth",
    "Celery task queue depth",
)
