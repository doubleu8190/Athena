"""工具批次的 LangGraph interrupt/resume 审批闸门。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.types import interrupt
from athena.contracts.statuses import AgentRunStatus

from ..state import AgentExecutionState

if TYPE_CHECKING:
    from ..langgraph_runtime import LangGraphRuntime


def _validate_resume(value: Any, batch: dict[str, Any]) -> dict[str, str]:
    if not isinstance(value, dict) or value.get("batch_id") != batch.get("batch_id"):
        raise ValueError("approval resume batch_id 不匹配")
    decisions = value.get("decisions")
    if not isinstance(decisions, dict):
        raise ValueError("approval resume 缺少 decisions")
    expected = {str(item["approval_id"]) for item in batch.get("approvals", [])}
    if set(decisions) != expected:
        raise ValueError("approval resume 必须一次性包含当前批次全部审批")
    normalized: dict[str, str] = {}
    for approval_id, decision in decisions.items():
        if decision not in {"approved", "denied", "cancelled"}:
            raise ValueError(f"非法审批决定: {decision}")
        normalized[str(approval_id)] = str(decision)
    return normalized


async def approval_gate(
    state: AgentExecutionState,
    config,
    *,
    graph_runtime: LangGraphRuntime,
    run_id: str,
) -> AgentExecutionState:
    """所有需要审批的工具一起中断；恢复时只接受完整批次结果。"""
    recoverable = state.get("recoverable", {})
    batch = recoverable.get("approval_batch") or {}
    approvals = list(batch.get("approvals", []))
    if not approvals:
        return {"recoverable": {"approval_decisions": {}}, "derived": {"status": "running"}}
    payload = {
        "kind": "tool_approval_batch",
        "batch_id": batch.get("batch_id"),
        "run_id": batch.get("run_id"),
        "approvals": approvals,
    }
    store = getattr(graph_runtime, "_agent_store", None)
    if store is not None:
        await store.update_run_status(run_id, AgentRunStatus.WAITING_APPROVAL)
    decisions = _validate_resume(interrupt(payload), batch)
    if store is not None:
        await store.update_run_status(run_id, AgentRunStatus.RUNNING)
    return {
        "recoverable": {
            "approval_decisions": decisions,
            "approval_batch": {**batch, "approvals": [{**item, "decision": decisions[item["approval_id"]]} for item in approvals]},
        },
        "derived": {"status": "running"},
    }
