import { BusinessApprovalCard } from "./BusinessApprovalCard";
import type { PendingSubagentApproval, PendingSubagentInput } from "./model";

export function subagentInputKey(item: PendingSubagentInput): string {
  return `${item.sub_run_id}:${item.input_request_id}`;
}

export function subagentApprovalKey(item: PendingSubagentApproval): string {
  return `${item.sub_run_id}:${item.call_id}`;
}

interface SubagentApprovalPanelProps {
  approvals: PendingSubagentApproval[];
  busyKeys: ReadonlySet<string>;
  errors: Record<string, string>;
  onDecision: (
    approval: PendingSubagentApproval,
    decision: "approve" | "reject",
  ) => void | Promise<void>;
  sessionId: string;
}

/**
 * 协作助手（子智能体）操作审批区：
 * 复用业务化确认卡片，标注「来自：子智能体名」；
 * 各子智能体的审批各自路由至对应 sub_run_id，提交态互不阻塞。
 */
export function SubagentApprovalPanel({
  approvals,
  busyKeys,
  errors,
  onDecision,
  sessionId,
}: SubagentApprovalPanelProps) {
  if (approvals.length === 0) {
    return null;
  }

  return (
    <section
      aria-labelledby="subagent-approval-heading"
      className="space-y-3"
    >
      <div className="flex items-center justify-between gap-2 px-0.5">
        <h2
          className="text-xs font-semibold text-warning"
          id="subagent-approval-heading"
        >
          协作任务操作审批（{approvals.length}）
        </h2>
        <span className="text-xs text-text-tertiary">
          各项确认相互独立、互不阻塞
        </span>
      </div>

      <div className="space-y-3">
        {approvals.map((approval, index) => {
          const key = subagentApprovalKey(approval);
          return (
            <BusinessApprovalCard
              approval={approval}
              busy={busyKeys.has(key)}
              error={errors[key] ?? null}
              index={index}
              key={key}
              onDecision={(decision) => onDecision(approval, decision)}
              sessionId={sessionId}
              subagentName={approval.subagent_name}
              subagentTaskSummary={approval.task_summary}
              total={approvals.length}
            />
          );
        })}
      </div>
    </section>
  );
}
