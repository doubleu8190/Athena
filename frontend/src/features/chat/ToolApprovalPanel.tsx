import { useState } from "react";

import { formatDateTime } from "../../shared/lib/format";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { useToolCallDetail } from "./hooks";
import { isSkillTool, type RunPendingApproval } from "./model";
import { SafeSkillProjection } from "./SafeSkillProjection";

function isSkillApproval(approval: RunPendingApproval): boolean {
  return (
    Boolean(approval.safe_skill_projection) ||
    isSkillTool(approval.model_name)
  );
}

function ApprovalRow({
  approval,
  busy,
  decisionPending,
  error,
  onDecision,
  runId,
  sessionId,
}: {
  approval: RunPendingApproval;
  busy: boolean;
  decisionPending: boolean;
  error: string | null;
  onDecision: (decision: "approve" | "reject") => void;
  runId: string;
  sessionId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const projectedSkill = approval.safe_skill_projection ?? null;
  const skillCall = isSkillApproval(approval);
  const details = useToolCallDetail(
    sessionId,
    runId,
    approval.call_id,
    expanded && !projectedSkill && !skillCall,
  );
  const detailProjection =
    details.data?.safe_skill_projection ?? null;
  const effectiveProjection = projectedSkill ?? detailProjection;
  const canApprove =
    !skillCall || Boolean(effectiveProjection?.can_approve);

  return (
    <li className="approval-row">
      <div className="approval-summary">
        <div>
          <strong>
            #{approval.ordinal} {approval.model_name}
          </strong>
          <span>
            {approval.tool_kind ?? "未标注类型"} · 请求于{" "}
            {formatDateTime(approval.requested_at)}
          </span>
        </div>
        <button
          aria-expanded={expanded}
          className="text-button"
          onClick={() => setExpanded((value) => !value)}
          type="button"
        >
          {expanded
            ? skillCall
              ? "收起安全详情"
              : "收起参数"
            : skillCall
              ? "查看安全详情"
              : "查看参数"}
        </button>
      </div>

      {effectiveProjection ? (
        <SafeSkillProjection
          projection={effectiveProjection}
          sessionId={sessionId}
          showParameters={expanded}
        />
      ) : null}

      {expanded ? (
        <div className="approval-detail">
          {effectiveProjection ? null : skillCall ? (
            <div className="skill-projection-unavailable" role="alert">
              服务端没有返回可审批的安全投影，页面不会读取或展示原始技能参数。请拒绝该调用，刷新后再重试。
            </div>
          ) : details.isPending ? (
            <p role="status">正在读取工具参数…</p>
          ) : details.isError ? (
            <ErrorBanner
              message="工具参数加载失败。"
              onRetry={() => void details.refetch()}
            />
          ) : details.data ? (
            <pre>
              {JSON.stringify(details.data.arguments ?? {}, null, 2)}
            </pre>
          ) : null}
        </div>
      ) : null}

      {error ? <p className="approval-error">{error}</p> : null}
      <div className="approval-actions">
        <button
          className="button-danger-outline"
          disabled={decisionPending}
          onClick={() => onDecision("reject")}
          type="button"
        >
          拒绝
        </button>
        <button
          className="button-primary"
          disabled={decisionPending || !canApprove}
          onClick={() => onDecision("approve")}
          type="button"
        >
          {busy
            ? "正在提交…"
            : decisionPending
              ? "等待其他审批…"
              : canApprove
                ? "批准"
                : "安全投影缺失，不可批准"}
        </button>
      </div>
    </li>
  );
}

interface ToolApprovalPanelProps {
  approvals: RunPendingApproval[];
  busyCallIds: ReadonlySet<string>;
  decisionPending: boolean;
  errors: Record<string, string>;
  onDecision: (
    approval: RunPendingApproval,
    decision: "approve" | "reject",
  ) => void;
  runId: string;
  sessionId: string;
}

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
      aria-labelledby="approval-title"
      className="approval-panel"
    >
      <header>
        <p className="empty-panel-kicker">Approval required</p>
        <h2 id="approval-title">等待工具审批</h2>
        <p>
          每次决定只作用于当前工具调用的持久化参数；最后一个决定完成后，Run
          会重新排队。
        </p>
      </header>
      {approvals.length === 0 ? (
        <p className="approval-empty">服务端当前没有返回待审批项。</p>
      ) : (
        <ul className="approval-list">
          {approvals.map((approval) => (
            <ApprovalRow
              approval={approval}
              busy={busyCallIds.has(approval.call_id)}
              decisionPending={decisionPending}
              error={errors[approval.call_id] ?? null}
              key={approval.call_id}
              onDecision={(decision) => onDecision(approval, decision)}
              runId={runId}
              sessionId={sessionId}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

