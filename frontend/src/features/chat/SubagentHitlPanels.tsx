import { useState } from "react";

import { formatDateTime } from "../../shared/lib/format";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { useToolCallDetail } from "./hooks";
import { MarkdownContent } from "./MarkdownContent";
import {
  isSkillTool,
  type PendingSubagentApproval,
  type PendingSubagentInput,
} from "./model";
import { SafeSkillProjection } from "./SafeSkillProjection";

export function subagentInputKey(item: PendingSubagentInput): string {
  return `${item.sub_run_id}:${item.input_request_id}`;
}

export function subagentApprovalKey(item: PendingSubagentApproval): string {
  return `${item.sub_run_id}:${item.call_id}`;
}

interface SubagentInputQueuePanelProps {
  activeInput: PendingSubagentInput | null;
  inputs: PendingSubagentInput[];
  onSelect: (item: PendingSubagentInput) => void;
}

export function SubagentInputQueuePanel({
  activeInput,
  inputs,
  onSelect,
}: SubagentInputQueuePanelProps) {
  if (inputs.length === 0) {
    return null;
  }

  const activeKey = activeInput ? subagentInputKey(activeInput) : null;

  return (
    <section
      aria-labelledby="subagent-input-queue-title"
      className="subagent-input-queue"
    >
      <header className="subagent-panel-header">
        <div>
          <p className="empty-panel-kicker">Subagent input requested</p>
          <h2 id="subagent-input-queue-title">
            待回答子智能体队列（{inputs.length}）
          </h2>
        </div>
        <span className="subagent-queue-hint">
          {inputs.length > 1
            ? "多个子智能体同时等待输入，请选择要回答的目标。"
            : "提交后将精确路由至该子智能体。"}
        </span>
      </header>

      <ul className="subagent-input-list">
        {inputs.map((item) => {
          const itemKey = subagentInputKey(item);
          const selected = itemKey === activeKey;
          return (
            <li key={itemKey}>
              <button
                aria-pressed={selected}
                className={`subagent-input-item${selected ? " is-selected" : ""}`}
                onClick={() => onSelect(item)}
                type="button"
              >
                <div className="subagent-input-item-head">
                  <span className="subagent-task-index">#{item.task_index}</span>
                  <strong>{item.subagent_name}</strong>
                  <code>{item.subagent_slug}</code>
                  {selected ? (
                    <span className="subagent-selected-pill">当前回答目标</span>
                  ) : null}
                </div>
                {item.task_summary ? (
                  <p className="subagent-task-summary">{item.task_summary}</p>
                ) : null}
                <p className="subagent-input-item-summary">
                  {item.question_text}
                </p>
              </button>
            </li>
          );
        })}
      </ul>

      {activeInput ? (
        <div className="subagent-active-question">
          <div className="subagent-active-question-label">
            正在回答 #{activeInput.task_index}「{activeInput.subagent_name}」的提问：
          </div>
          {activeInput.task_summary ? (
            <p className="subagent-task-summary">{activeInput.task_summary}</p>
          ) : null}
          <MarkdownContent text={activeInput.question_text} />
        </div>
      ) : null}
    </section>
  );
}

function SubagentApprovalRow({
  approval,
  busy,
  error,
  onDecision,
  sessionId,
}: {
  approval: PendingSubagentApproval;
  busy: boolean;
  error: string | null;
  onDecision: (decision: "approve" | "reject") => void;
  sessionId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const projectedSkill = approval.safe_skill_projection ?? null;
  const skillCall =
    Boolean(projectedSkill) || isSkillTool(approval.model_name);
  // 子智能体工具详情必须向对应 sub_run_id 发起查询。
  const details = useToolCallDetail(
    sessionId,
    approval.sub_run_id,
    approval.call_id,
    expanded && !projectedSkill && !skillCall,
  );
  const detailProjection = details.data?.safe_skill_projection ?? null;
  const effectiveProjection = projectedSkill ?? detailProjection;
  const canApprove = !skillCall || Boolean(effectiveProjection?.can_approve);

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
          className="text-button detail-toggle"
          onClick={() => setExpanded((value) => !value)}
          type="button"
        >
          <Icon
            className="detail-toggle-chevron"
            name="chevron"
            size={12}
          />
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
            <pre>{JSON.stringify(details.data.arguments ?? {}, null, 2)}</pre>
          ) : null}
        </div>
      ) : null}

      {error ? <p className="approval-error">{error}</p> : null}
      <div className="approval-actions">
        <button
          className="button-danger-outline"
          disabled={busy}
          onClick={() => onDecision("reject")}
          type="button"
        >
          拒绝
        </button>
        <button
          className="button-primary"
          disabled={busy || !canApprove}
          onClick={() => onDecision("approve")}
          type="button"
        >
          {busy
            ? "正在提交…"
            : canApprove
              ? "批准"
              : "安全投影缺失，不可批准"}
        </button>
      </div>
    </li>
  );
}

interface SubagentApprovalPanelProps {
  approvals: PendingSubagentApproval[];
  busyKeys: ReadonlySet<string>;
  errors: Record<string, string>;
  onDecision: (
    approval: PendingSubagentApproval,
    decision: "approve" | "reject",
  ) => void;
  sessionId: string;
}

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

  // 按 sub_run_id 分组，确保不同子智能体的工具审批互不阻塞且归属清晰。
  const groups = new Map<
    string,
    {
      sub_run_id: string;
      subagent_name: string;
      subagent_slug: string;
      task_index: number;
      task_summary: string;
      items: PendingSubagentApproval[];
    }
  >();
  for (const item of approvals) {
    const existing = groups.get(item.sub_run_id);
    if (existing) {
      existing.items.push(item);
    } else {
      groups.set(item.sub_run_id, {
        sub_run_id: item.sub_run_id,
        subagent_name: item.subagent_name,
        subagent_slug: item.subagent_slug,
        task_index: item.task_index,
        task_summary: item.task_summary,
        items: [item],
      });
    }
  }

  return (
    <section
      aria-labelledby="subagent-approval-title"
      className="approval-panel subagent-approval-panel"
    >
      <header>
        <p className="empty-panel-kicker">Subagent approvals</p>
        <h2 id="subagent-approval-title">
          子智能体工具审批（{approvals.length}）
        </h2>
        <p>
          按子智能体分组展示待审批调用；各子智能体的审批相互独立、互不阻塞。
        </p>
      </header>

      <div className="subagent-approval-groups">
        {Array.from(groups.values()).map((group) => (
          <div className="subagent-approval-group" key={group.sub_run_id}>
            <div className="subagent-approval-group-header">
              <span className="subagent-task-index">#{group.task_index}</span>
              <strong>{group.subagent_name}</strong>
              <code>{group.subagent_slug}</code>
              <span>{group.items.length} 项待审批</span>
            </div>
            {group.task_summary ? (
              <p className="subagent-task-summary">{group.task_summary}</p>
            ) : null}
            <ul className="approval-list">
              {group.items.map((approval) => {
                const key = subagentApprovalKey(approval);
                return (
                  <SubagentApprovalRow
                    approval={approval}
                    busy={busyKeys.has(key)}
                    error={errors[key] ?? null}
                    key={key}
                    onDecision={(decision) => onDecision(approval, decision)}
                    sessionId={sessionId}
                  />
                );
              })}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}
