"""准备工具批次及其审批请求，不执行任何工具。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from athena.models.tool import RiskLevel

from ..state import AgentExecutionState

if TYPE_CHECKING:
    from ..langgraph_runtime import LangGraphRuntime


async def prepare_tool_batch(
    state: AgentExecutionState,
    config,
    *,
    graph_runtime: LangGraphRuntime,
    session_id: str,
    run_id: str,
) -> AgentExecutionState:
    """为当前工具批次创建（或恢复）全部审批请求。"""
    recoverable = state.get("recoverable", {})
    calls = list(recoverable.get("pending_tool_calls", []))
    batch_id = (
        recoverable.get("approval_batch", {}).get("batch_id")
        if recoverable.get("approval_batch")
        else None
    )
    # ``turn_count`` lives inside the recoverable execution container.  Using
    # the outer state here would collapse every tool round to ``:0`` and make
    # later approval batches reuse the first batch's durable records.
    turn_count = int(recoverable.get("turn_count", 0))
    batch_id = batch_id or f"{run_id}:tool-batch:{turn_count}"
    approvals = dict(recoverable.get("approval_ids", {}))
    approval_items: list[dict[str, object]] = []
    manager = graph_runtime._tool_manager
    approval_manager = graph_runtime._approval_manager
    for index, call in enumerate(calls):
        call_id = str(call.get("id") or f"{batch_id}:{index}")
        call["id"] = call_id
        if not manager.require_approval(str(call.get("name", ""))):
            continue
        approval_id = approvals.get(call_id) or f"{batch_id}:{index}:approval"
        # 与执行器的稳定工具账本 ID 保持一致，审批记录可直接关联到
        # 工具节点创建的 ToolCallRecord，无需执行后回写。
        tool_batch_call_id = (
            f"{run_id}:tool-batch:{recoverable.get('turn_count', 0)}:{index}"
        )
        tool_record_id = (
            f"{run_id}:tool-record:{tool_batch_call_id}:{call_id}"
        )
        request = await approval_manager.request_approval(
            approval_id=approval_id,
            tool_name=str(call.get("name", "")),
            arguments=dict(call.get("args") or {}),
            risk_level=RiskLevel(manager.get_risk_level(str(call.get("name", "")))),
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_record_id,
            approval_batch_id=batch_id,
        )
        approvals[call_id] = request.id
        call["approval_id"] = request.id
        approval_items.append(
            {
                "approval_id": request.id,
                "tool_call_id": call_id,
                "tool_name": call.get("name", ""),
                "arguments": call.get("args") or {},
                "risk_level": manager.get_risk_level(str(call.get("name", ""))),
                "decision": None,
            }
        )
    return {
        "recoverable": {
            "pending_tool_calls": calls,
            "approval_ids": approvals,
            "approval_batch": {
            "batch_id": batch_id,
            "run_id": run_id,
            "approvals": approval_items,
            },
        },
        "derived": {"status": "waiting_approval" if approval_items else "running"},
    }
