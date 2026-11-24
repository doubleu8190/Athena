import { ChevronDown, RefreshCw, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { Badge, Button, InlineAlert, Skeleton, cn } from "../../shared/ui";
import { useToolCallDetail } from "./hooks";
import {
  isSkillTool,
  type FormattedToolArgument,
  type SafeSkillApprovalProjection,
} from "./model";
import { SafeSkillProjection } from "./SafeSkillProjection";

export interface BusinessApprovalItem {
  call_id: string;
  model_name: string;
  display_name?: string | null;
  approval_summary?: string | null;
  formatted_arguments?: FormattedToolArgument[];
  safe_skill_projection: SafeSkillApprovalProjection | null;
}

export interface BusinessApprovalCardProps {
  approval: BusinessApprovalItem;
  busy: boolean;
  error: string | null;
  index: number;
  onDecision: (decision: "approve" | "reject") => void;
  /** 主 Run 审批存在全局串行锁时，锁定同批其余卡片以防止并发响应乱序覆盖。 */
  otherCommandPending?: boolean;
  runId: string;
  sessionId: string;
  subagentName?: string;
  subagentTaskSummary?: string;
  total: number;
}

/** 将兜底拉取的普通工具参数对象格式化为人类可读的键值对列表，避免向业务员工暴露原始 JSON 墙。 */
function formatFallbackArgumentEntries(
  args: Record<string, unknown> | null,
): Array<{ key: string; value: string }> {
  if (!args) {
    return [];
  }
  return Object.entries(args).map(([key, raw]) => {
    if (raw === null || raw === undefined) {
      return { key, value: "—" };
    }
    if (typeof raw === "boolean") {
      return { key, value: raw ? "是" : "否" };
    }
    if (typeof raw === "string" || typeof raw === "number") {
      return { key, value: String(raw) };
    }
    if (
      Array.isArray(raw) &&
      raw.every(
        (item) =>
          typeof item === "string" ||
          typeof item === "number" ||
          typeof item === "boolean",
      )
    ) {
      return { key, value: raw.join("、") };
    }
    return { key, value: JSON.stringify(raw) };
  });
}

/**
 * 业务化工具操作确认卡片（主 Run 与子智能体审批统一复用）：
 * - 高辨识度柔和琥珀表面，突出工具业务名、一句话业务摘要与结构化参数表；
 * - 严禁前端对摘要执行危险模板插值，原样呈现后端生成的 approval_summary 与 formatted_arguments；
 * - 技能脚本调用严格坚守 SafeSkillProjection 安全底线：缺少安全投影时绝不请求原始参数，且强制禁用确认执行。
 */
export function BusinessApprovalCard({
  approval,
  busy,
  error,
  index,
  onDecision,
  otherCommandPending = false,
  runId,
  sessionId,
  subagentName,
  subagentTaskSummary,
  total,
}: BusinessApprovalCardProps) {
  const [expanded, setExpanded] = useState(false);

  const projectedSkill = approval.safe_skill_projection ?? null;
  const initialSkillCall =
    Boolean(projectedSkill) || isSkillTool(approval.model_name);
  const formattedArguments = approval.formatted_arguments ?? [];
  const hasFormattedArguments = formattedArguments.length > 0;

  // 一层安全门禁：仅当非已知技能工具、未下发 formatted_arguments 且用户主动展开时才按需查询工具详情。
  const shouldFetchFallbackDetail =
    expanded && !initialSkillCall && !hasFormattedArguments;
  const details = useToolCallDetail(
    sessionId,
    runId,
    approval.call_id,
    shouldFetchFallbackDetail,
  );

  // 二层安全纵深防御：兜底详情若返回了 safe_skill_projection 或技能 tool_id/model_name，
  // 立即按技能调用收口，严禁渲染原始 arguments，并同步收紧确认按钮门禁。
  const detailProjection = details.data?.safe_skill_projection ?? null;
  const effectiveProjection = projectedSkill ?? detailProjection;
  const detailSkillCall =
    Boolean(detailProjection) ||
    (details.data
      ? isSkillTool(details.data.model_name, details.data.tool_id)
      : false);
  const skillCall = initialSkillCall || detailSkillCall;

  // 安全铁律：凡识别为技能工具，缺失安全投影或 can_approve=false 时均禁止确认执行，仅允许拒绝。
  const canApprove = !skillCall || Boolean(effectiveProjection?.can_approve);
  const title = approval.display_name?.trim() || approval.model_name;
  const summary = approval.approval_summary?.trim() || null;

  const fallbackEntries = skillCall
    ? []
    : formatFallbackArgumentEntries(details.data?.arguments ?? null);

  const rejectDisabled = busy || otherCommandPending;
  const approveDisabled = busy || otherCommandPending || !canApprove;
  const approveLabel = busy
    ? "正在提交…"
    : otherCommandPending
      ? "等待其他审批…"
      : !canApprove
        ? "安全投影缺失，不可批准"
        : "确认执行";

  return (
    <article
      aria-label={`操作确认：${title}`}
      className="rounded-xl border border-warning-border bg-warning-bg/50 p-4 shadow-xs"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2.5">
          <span
            aria-hidden="true"
            className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-warning-border bg-warning-bg text-warning"
          >
            <ShieldCheck className="h-4 w-4" />
          </span>
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold text-text-primary">
                {title}
              </h3>
              {subagentName ? (
                <Badge variant="warning">来自：{subagentName}</Badge>
              ) : null}
            </div>
            {subagentTaskSummary ? (
              <p className="text-xs text-text-secondary">
                {subagentTaskSummary}
              </p>
            ) : null}
          </div>
        </div>

        <span className="shrink-0 rounded-full border border-warning-border bg-canvas/80 px-2 py-0.5 text-xs font-medium text-warning tabular-nums">
          {index + 1} / {total}
        </span>
      </div>

      {/* 后端已完成安全占位符替换与长度截断，前端直接渲染业务一句话说明。 */}
      {summary ? (
        <p className="mt-3 text-sm leading-relaxed text-text-primary">
          {summary}
        </p>
      ) : null}

      {/* 分支 1：已知或经兜底详情识别为技能脚本调用，严格使用 SafeSkillProjection 安全视图或阻断提示，绝不渲染原始 arguments。 */}
      {skillCall ? (
        effectiveProjection ? (
          <div className="mt-2">
            <button
              aria-expanded={expanded}
              className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-xs font-medium text-warning transition-colors hover:bg-warning-bg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
              data-compact-control="true"
              onClick={() => setExpanded((value) => !value)}
              type="button"
            >
              <ChevronDown
                aria-hidden="true"
                className={cn(
                  "h-3.5 w-3.5 transition-transform duration-150",
                  expanded && "rotate-180",
                )}
              />
              {expanded ? "收起安全详情" : "查看安全详情"}
            </button>
            <SafeSkillProjection
              projection={effectiveProjection}
              sessionId={sessionId}
              showParameters={expanded}
            />
          </div>
        ) : (
          <div className="mt-3">
            <InlineAlert
              description="服务端没有返回可审批的安全投影，页面不会读取或展示原始技能参数。请拒绝该调用，刷新后再重试。"
              variant="warning"
            />
          </div>
        )
      ) : hasFormattedArguments ? (
        /* 分支 2：非技能工具优先直接遍历后端下发的 formatted_arguments 渲染响应式 2 列 / 1 列键值表。 */
        <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-2 rounded-lg border border-warning-border/70 bg-canvas/90 px-3.5 py-2.5 sm:grid-cols-2">
          {formattedArguments.map((arg) => (
            <div
              className="grid min-w-0 grid-cols-[minmax(72px,auto)_1fr] items-baseline gap-2.5"
              key={arg.key}
            >
              <dt className="text-xs font-medium text-text-secondary">
                {arg.label}
              </dt>
              <dd className="min-w-0 break-words text-xs font-medium text-text-primary">
                {arg.value}
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        /* 分支 3：非技能且未下发 formatted_arguments 时按需展开兜底查询，禁止直接裸露 JSON 墙。 */
        <div className="mt-2.5">
          <button
            aria-expanded={expanded}
            className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-xs font-medium text-warning transition-colors hover:bg-warning-bg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
            data-compact-control="true"
            onClick={() => setExpanded((value) => !value)}
            type="button"
          >
            <ChevronDown
              aria-hidden="true"
              className={cn(
                "h-3.5 w-3.5 transition-transform duration-150",
                expanded && "rotate-180",
              )}
            />
            {expanded ? "收起参数" : "查看参数"}
          </button>

          {expanded ? (
            <div className="mt-2 rounded-lg border border-warning-border/70 bg-canvas/90 p-3">
              {details.isPending ? (
                <div className="space-y-2" role="status">
                  <span className="sr-only">正在读取工具参数…</span>
                  <Skeleton className="h-4 w-2/3" />
                  <Skeleton className="h-4 w-1/2" />
                </div>
              ) : details.isError ? (
                <InlineAlert
                  action={
                    <Button
                      onClick={() => void details.refetch()}
                      size="xs"
                      variant="outline"
                    >
                      <RefreshCw aria-hidden="true" className="h-3 w-3" />
                      重试
                    </Button>
                  }
                  description="操作参数加载失败，请重试。"
                  variant="danger"
                />
              ) : fallbackEntries.length === 0 ? (
                <p className="text-xs text-text-tertiary">
                  本次操作未携带额外参数。
                </p>
              ) : (
                <dl className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2">
                  {fallbackEntries.map((entry) => (
                    <div
                      className="grid min-w-0 grid-cols-[minmax(72px,auto)_1fr] items-baseline gap-2.5"
                      key={entry.key}
                    >
                      <dt className="text-xs font-medium text-text-secondary">
                        {entry.key}
                      </dt>
                      <dd className="min-w-0 break-words text-xs font-medium text-text-primary">
                        {entry.value}
                      </dd>
                    </div>
                  ))}
                </dl>
              )}
            </div>
          ) : null}
        </div>
      )}

      {error ? (
        <div className="mt-3">
          <InlineAlert description={error} variant="danger" />
        </div>
      ) : null}

      <div className="mt-3.5 flex flex-wrap items-center justify-end gap-2">
        <Button
          disabled={rejectDisabled}
          onClick={() => onDecision("reject")}
          size="sm"
          variant="danger-outline"
        >
          拒绝
        </Button>
        <Button
          disabled={approveDisabled}
          loading={busy}
          onClick={() => onDecision("approve")}
          size="sm"
          variant="primary"
        >
          {approveLabel}
        </Button>
      </div>
    </article>
  );
}
