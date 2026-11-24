import { BusinessApprovalCard } from "./BusinessApprovalCard";
import type { RunPendingApproval } from "./model";

interface ToolApprovalPanelProps {
  approvals: RunPendingApproval[];
  busyCallIds: ReadonlySet<string>;
  decisionPending: boolean;
  errors: Record<string, string>;
  onDecision: (
    approval: RunPendingApproval,
    decision: "approve" | "reject",
  ) => void | Promise<void>;
  runId: string;
  sessionId: string;
}

/** 主对话任务工具操作审批区：逐项确认后自动继续推进当前任务。 */
export function ToolApprovalPanel({
  approvals,
  busyCallIds,
  decisionPending,
  errors,
  onDecision,
  runId,
  sessionId,
}: ToolApprovalPanelProps) {
  return (
    <section
      aria-busy={decisionPending}
      aria-labelledby="approval-panel-heading"
      className="space-y-3"
    >
      <div className="flex items-center justify-between gap-2 px-0.5">
        <h2
          className="text-xs font-semibold text-warning"
          id="approval-panel-heading"
        >
          等待工具审批
        </h2>
        {approvals.length > 0 ? (
          <span className="text-xs text-text-tertiary">
            共 {approvals.length} 项待确认
          </span>
        ) : null}
      </div>

      {approvals.length === 0 ? (
        <p className="rounded-xl border border-warning-border/70 bg-warning-bg/30 px-4 py-3 text-xs text-text-secondary">
          当前暂无待确认的操作，正在同步最新进展。
        </p>
      ) : (
        <div className="space-y-3">
          {approvals.map((approval, index) => {
            const busy = busyCallIds.has(approval.call_id);
            return (
              <BusinessApprovalCard
                approval={approval}
                busy={busy}
                error={errors[approval.call_id] ?? null}
                index={index}
                key={approval.call_id}
                onDecision={(decision) => onDecision(approval, decision)}
                otherCommandPending={decisionPending && !busy}
                runId={runId}
                sessionId={sessionId}
                total={approvals.length}
              />
            );
          })}
        </div>
      )}
    </section>
  );
}
