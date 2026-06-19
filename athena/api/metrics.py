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
