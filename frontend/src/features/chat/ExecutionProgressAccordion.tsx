import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Layers,
  Loader2,
  RefreshCw,
  Wrench,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { formatBytes, formatDateTime } from "../../shared/lib/format";
import {
  Badge,
  Button,
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
  InlineAlert,
  Skeleton,
  cn,
} from "../../shared/ui";
import { MarkdownContent } from "../../shared/ui/MarkdownContent";
import { fileDisplayName } from "../files/model";
import { SessionFileDownloadButton } from "../files/SessionFileDownloadButton";
import { runFailureReason } from "./FailedRunCard";
import { useRunDetails, useRunToolCalls, useToolCallDetail } from "./hooks";
import {
  isActiveRunStatus,
  isSkillTool,
  isTerminalRunStatus,
  type Run,
  type RunStatus,
  type RunToolCallSummary,
  type SubagentTaskSummary,
} from "./model";
import { SafeSkillProjection } from "./SafeSkillProjection";

const BUILTIN_TOOL_DISPLAY_NAMES: Record<string, string> = {
  ask_user_input: "请求补充信息",
  builtin_ask_user_input: "请求补充信息",
  builtin_dispatch_subagents: "协调协作助手",
  builtin_knowledge_search: "查阅企业知识库",
  builtin_save_user_memory: "记录个人工作偏好",
  builtin_skill_execute: "执行技能脚本",
  builtin_skill_load: "加载技能说明",
  builtin_skill_read_resource: "读取技能资料",
  dispatch_subagents: "协调协作助手",
  knowledge_search: "查阅企业知识库",
  save_user_memory: "记录个人工作偏好",
  skill_execute: "执行技能脚本",
  skill_load: "加载技能说明",
  skill_read_resource: "读取技能资料",
};

function resolveToolDisplayName(
  call: Pick<RunToolCallSummary, "display_name" | "model_name" | "tool_id">,
  approvalDisplayName?: string | null,
): string {
  const explicit = call.display_name?.trim() || approvalDisplayName?.trim();
  if (explicit) {
    return explicit;
  }
  if (call.tool_id && Object.hasOwn(BUILTIN_TOOL_DISPLAY_NAMES, call.tool_id)) {
    return BUILTIN_TOOL_DISPLAY_NAMES[call.tool_id];
  }
  const normalized = call.model_name.toLowerCase();
  if (Object.hasOwn(BUILTIN_TOOL_DISPLAY_NAMES, normalized)) {
    return BUILTIN_TOOL_DISPLAY_NAMES[normalized];
  }
  return call.model_name;
}

function formatDurationMs(durationMs: number | null): string | null {
  if (durationMs === null || durationMs < 0 || Number.isNaN(durationMs)) {
    return null;
  }
  if (durationMs < 1_000) {
    return `${Math.round(durationMs)} ms`;
  }
  return `${(durationMs / 1_000).toFixed(1)}s`;
}

function computeRunDurationMs(
  run: Run,
  toolCalls: RunToolCallSummary[],
): number | null {
  if (run.first_started_at && run.finished_at) {
    const start = Date.parse(run.first_started_at);
    const end = Date.parse(run.finished_at);
    if (!Number.isNaN(start) && !Number.isNaN(end) && end >= start) {
      return end - start;
    }
  }
  const durations = toolCalls
    .map((call) => call.duration_ms)
    .filter((value): value is number => value !== null && value >= 0);
  if (durations.length === 0) {
    return null;
  }
  return durations.reduce((sum, value) => sum + value, 0);
}

function isTerminalToolStatus(status: string): boolean {
  return (
    status === "completed" ||
    status === "succeeded" ||
    status === "success" ||
    status === "ok" ||
    status === "failed" ||
    status === "error" ||
    status === "rejected" ||
    status === "denied" ||
    status === "cancelled" ||
    status === "unknown"
  );
}

function toolStatusBadge(status: string): {
  label: string;
  variant: "brand" | "danger" | "neutral" | "success" | "warning";
} {
  switch (status) {
    case "completed":
    case "succeeded":
    case "success":
    case "ok":
      return { label: "已完成", variant: "success" };
    case "rejected":
    case "denied":
      return { label: "已拒绝", variant: "warning" };
    case "waiting_for_approval":
    case "pending_approval":
      return { label: "待确认", variant: "warning" };
    case "failed":
    case "error":
    case "unknown":
      return { label: "执行失败", variant: "danger" };
    case "cancelled":
      return { label: "已取消", variant: "neutral" };
    default:
      return { label: "进行中", variant: "brand" };
  }
}

function subagentStatusBadge(status: RunStatus): {
  label: string;
  variant: "brand" | "danger" | "info" | "neutral" | "success" | "warning";
} {
  switch (status) {
    case "completed":
      return { label: "已完成", variant: "success" };
    case "failed":
      return { label: "执行失败", variant: "danger" };
    case "cancelled":
      return { label: "已取消", variant: "neutral" };
    case "waiting_for_input":
      return { label: "等待补充", variant: "info" };
    case "waiting_for_approval":
      return { label: "等待确认", variant: "warning" };
    case "cancelling":
      return { label: "正在停止", variant: "warning" };
    case "queued":
      return { label: "排队中", variant: "neutral" };
    default:
      return { label: "进行中", variant: "brand" };
  }
}

function interactionStatusLabel(status: string): string {
  switch (status) {
    case "pending":
      return "待回答";
    case "answered":
      return "已回答";
    case "cancelled":
    case "closed":
      return "已关闭";
    default:
      return status;
  }
}

/** 将普通工具的结构化对象格式化为可读键值项，不直接向业务员工堆砌裸 JSON。 */
function formatReadableEntries(
  record: Record<string, unknown> | null | undefined,
): Array<{ key: string; value: string }> {
  if (!record) {
    return [];
  }
  return Object.entries(record).map(([key, raw]) => {
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

function StepArtifactList({
  artifacts,
  sessionId,
}: {
  artifacts: NonNullable<
    ReturnType<typeof useToolCallDetail>["data"]
  >["artifacts"];
  sessionId: string;
}) {
  if (!artifacts || artifacts.length === 0) {
    return null;
  }
  return (
    <div className="space-y-1.5 border-t border-border-subtle pt-2.5">
      <div className="text-[11px] font-semibold text-text-secondary">
        交付文件
      </div>
      <ul className="space-y-1.5">
        {artifacts.map((artifact) => (
          <li
            className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border-subtle bg-subtle/60 px-2.5 py-1.5"
            key={artifact.file_id}
          >
            <span className="min-w-0 flex-1">
              <strong className="block truncate text-xs font-medium text-text-primary">
                {fileDisplayName(artifact)}
              </strong>
              <small className="block truncate text-[11px] text-text-tertiary">
                {artifact.filename} · {formatBytes(artifact.size_bytes)}
              </small>
            </span>
            <SessionFileDownloadButton
              compact
              fileId={artifact.file_id}
              filename={fileDisplayName(artifact)}
              sessionId={sessionId}
            />
          </li>
        ))}
      </ul>
    </div>
  );
}

/** 单个工具步骤行：点击展开后才按需懒加载 useToolCallDetail，并坚守技能安全投影铁律。 */
function ToolStepRow({
  approvalDisplayName,
  call,
  runId,
  sessionId,
}: {
  approvalDisplayName?: string | null;
  call: RunToolCallSummary;
  runId: string;
  sessionId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const details = useToolCallDetail(sessionId, runId, call.id, expanded);

  // 安全铁律：凡属技能工具（按 tool_id / model_name 或后端返回 safe_skill_projection 任一命中的），
  // 严格只渲染 SafeSkillProjection；缺失安全投影时展示安全阻断提示，绝不回退展示原始 arguments 或 outcome。
  const skillCall =
    isSkillTool(call.model_name, call.tool_id) ||
    Boolean(details.data?.safe_skill_projection);
  const skillProjection = details.data?.safe_skill_projection ?? null;
  const artifacts = details.data?.artifacts ?? [];
  const displayName = resolveToolDisplayName(call, approvalDisplayName);
  const statusInfo = toolStatusBadge(call.status);
  const durationLabel = formatDurationMs(call.duration_ms);
  const argEntries = formatReadableEntries(details.data?.arguments);
  const outcomeDataEntries = formatReadableEntries(details.data?.outcome?.data);

  return (
    <li className="rounded-lg border border-border-subtle bg-canvas">
      <button
        aria-expanded={expanded}
        className="flex w-full items-center justify-between gap-2.5 rounded-lg px-3 py-2 text-left transition-colors hover:bg-subtle/70 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
        data-compact-control="true"
        onClick={() => setExpanded((value) => !value)}
        type="button"
      >
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <Wrench
            aria-hidden="true"
            className="h-3.5 w-3.5 shrink-0 text-text-tertiary"
          />
          <span className="truncate text-xs font-medium text-text-primary">
            {displayName}
          </span>
          <Badge className="shrink-0" variant={statusInfo.variant}>
            {statusInfo.label}
          </Badge>
        </div>
        <div className="flex shrink-0 items-center gap-2 text-[11px] text-text-tertiary">
          {durationLabel ? (
            <span className="tabular-nums">{durationLabel}</span>
          ) : null}
          <ChevronRight
            aria-hidden="true"
            className={cn(
              "h-3.5 w-3.5 transition-transform duration-150",
              expanded && "rotate-90",
            )}
          />
        </div>
      </button>

      {expanded ? (
        <div className="space-y-3 border-t border-border-subtle bg-subtle/35 px-3.5 py-3 text-xs">
          {details.isPending ? (
            <div className="space-y-2" role="status">
              <span className="sr-only">正在读取步骤详情…</span>
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-4 w-3/4" />
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
              description="步骤详情加载失败，请重试。"
              variant="danger"
            />
          ) : details.data && skillCall ? (
            <>
              {skillProjection ? (
                <SafeSkillProjection
                  projection={skillProjection}
                  sessionId={sessionId}
                />
              ) : (
                <InlineAlert
                  description="该技能调用缺少服务端安全投影。普通用户详情不会回退展示原始参数、输出或日志。"
                  variant="warning"
                />
              )}
              <StepArtifactList artifacts={artifacts} sessionId={sessionId} />
            </>
          ) : details.data ? (
            <>
              <div className="space-y-1.5">
                <div className="text-[11px] font-semibold text-text-secondary">
                  调用参数
                </div>
                {argEntries.length === 0 ? (
                  <p className="text-text-tertiary">无额外参数。</p>
                ) : (
                  <dl className="grid grid-cols-1 gap-x-5 gap-y-1.5 rounded-lg border border-border-subtle bg-canvas p-2.5 sm:grid-cols-2">
                    {argEntries.map((entry) => (
                      <div
                        className="grid min-w-0 grid-cols-[minmax(68px,auto)_1fr] items-baseline gap-2"
                        key={entry.key}
                      >
                        <dt className="text-text-secondary">{entry.key}</dt>
                        <dd className="min-w-0 break-words font-medium text-text-primary">
                          {entry.value}
                        </dd>
                      </div>
                    ))}
                  </dl>
                )}
              </div>

              {details.data.outcome ? (
                <div className="space-y-1.5 border-t border-border-subtle pt-2.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[11px] font-semibold text-text-secondary">
                      执行结果
                    </span>
                    <Badge
                      variant={
                        details.data.outcome.status === "success" ||
                        details.data.outcome.status === "ok"
                          ? "success"
                          : "warning"
                      }
                    >
                      {details.data.outcome.status === "success" ||
                      details.data.outcome.status === "ok"
                        ? "执行成功"
                        : "执行异常"}
                    </Badge>
                    {details.data.truncated || details.data.outcome.truncated ? (
                      <span className="text-[11px] text-text-tertiary">
                        部分结果已省略展示
                      </span>
                    ) : null}
                  </div>
                  {details.data.outcome.message ? (
                    <p className="leading-relaxed text-text-primary">
                      {details.data.outcome.message}
                    </p>
                  ) : null}
                  {outcomeDataEntries.length > 0 ? (
                    <dl className="grid grid-cols-1 gap-x-5 gap-y-1.5 rounded-lg border border-border-subtle bg-canvas p-2.5 sm:grid-cols-2">
                      {outcomeDataEntries.map((entry) => (
                        <div
                          className="grid min-w-0 grid-cols-[minmax(68px,auto)_1fr] items-baseline gap-2"
                          key={entry.key}
                        >
                          <dt className="text-text-secondary">{entry.key}</dt>
                          <dd className="min-w-0 break-words font-medium text-text-primary">
                            {entry.value}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  ) : null}
                </div>
              ) : (
                <p className="text-text-tertiary">该步骤尚未返回最终结果。</p>
              )}

              <StepArtifactList artifacts={artifacts} sessionId={sessionId} />
            </>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

/** 嵌套协作任务卡片（替代旧独立 SubagentExecutionPanel）：隐藏底层 slug/run_id，按需懒加载详情与工具步骤。 */
function NestedSubagentTaskRow({
  sessionId,
  task,
}: {
  sessionId: string;
  task: SubagentTaskSummary;
}) {
  const [expanded, setExpanded] = useState(false);
  // 仅在用户主动展开协作任务详情时按需请求子任务详情与步骤列表。
  const detailQuery = useRunDetails(sessionId, task.sub_run_id, expanded);
  const toolCallsQuery = useRunToolCalls(sessionId, task.sub_run_id, expanded);
  const toolCalls = (toolCallsQuery.data?.pages ?? []).flatMap(
    (page) => page.items,
  );
  const inputInteractions = detailQuery.data?.input_interactions ?? [];
  const badge = subagentStatusBadge(task.status);

  const durationMs =
    task.first_started_at && task.finished_at
      ? Date.parse(task.finished_at) - Date.parse(task.first_started_at)
      : null;
  const durationLabel = formatDurationMs(
    durationMs !== null && !Number.isNaN(durationMs) ? durationMs : null,
  );

  return (
    <li className="rounded-lg border border-border-subtle bg-canvas">
      <div className="flex flex-wrap items-start justify-between gap-2 px-3 py-2.5">
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded bg-subtle px-1.5 py-0.5 text-[11px] font-medium text-text-tertiary tabular-nums">
              #{task.task_index}
            </span>
            <span className="text-xs font-semibold text-text-primary">
              {task.subagent_name}
            </span>
            <Badge variant={badge.variant}>{badge.label}</Badge>
            {durationLabel ? (
              <span className="text-[11px] text-text-tertiary tabular-nums">
                {durationLabel}
              </span>
            ) : null}
          </div>
          {task.task_summary ? (
            <p className="text-xs leading-relaxed text-text-secondary">
              {task.task_summary}
            </p>
          ) : null}
          {task.safe_error_code ? (
            <p className="text-xs text-danger">
              {runFailureReason(task.safe_error_code)}
            </p>
          ) : null}
        </div>

        <button
          aria-expanded={expanded}
          className="inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-text-secondary transition-colors hover:bg-subtle hover:text-text-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
          data-compact-control="true"
          onClick={() => setExpanded((value) => !value)}
          type="button"
        >
          <span>{expanded ? "收起详情" : "查看详情"}</span>
          <ChevronDown
            aria-hidden="true"
            className={cn(
              "h-3.5 w-3.5 transition-transform duration-150",
              expanded && "rotate-180",
            )}
          />
        </button>
      </div>

      {expanded ? (
        <div className="space-y-3 border-t border-border-subtle bg-subtle/35 px-3.5 py-3 text-xs">
          {detailQuery.isPending || toolCallsQuery.isPending ? (
            <div className="space-y-2" role="status">
              <span className="sr-only">正在加载协作任务详情…</span>
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-1/2" />
            </div>
          ) : detailQuery.isError ? (
            <InlineAlert
              action={
                <Button
                  onClick={() => void detailQuery.refetch()}
                  size="xs"
                  variant="outline"
                >
                  重试
                </Button>
              }
              description="协作任务详情加载失败。"
              variant="danger"
            />
          ) : toolCallsQuery.isError ? (
            <InlineAlert
              action={
                <Button
                  onClick={() => void toolCallsQuery.refetch()}
                  size="xs"
                  variant="outline"
                >
                  重试
                </Button>
              }
              description="协作任务步骤加载失败。"
              variant="danger"
            />
          ) : (
            <>
              {inputInteractions.length > 0 ? (
                <div className="space-y-2">
                  <h4 className="text-[11px] font-semibold text-text-secondary">
                    补充问答记录
                  </h4>
                  <ol className="space-y-2">
                    {inputInteractions.map((interaction) => (
                      <li
                        className="space-y-1.5 rounded-lg border border-border-subtle bg-canvas p-2.5"
                        key={interaction.input_request_id}
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <Badge
                            variant={
                              interaction.status === "answered"
                                ? "success"
                                : interaction.status === "pending"
                                  ? "info"
                                  : "neutral"
                            }
                          >
                            {interactionStatusLabel(interaction.status)}
                          </Badge>
                          <span className="text-[11px] text-text-tertiary">
                            {formatDateTime(interaction.created_at)}
                          </span>
                        </div>
                        <div>
                          <div className="text-[11px] font-medium text-text-tertiary">
                            提问
                          </div>
                          <MarkdownContent
                            className="text-xs"
                            text={interaction.question_text}
                          />
                        </div>
                        <div>
                          <div className="text-[11px] font-medium text-text-tertiary">
                            回答
                          </div>
                          {interaction.answer_text ? (
                            <MarkdownContent
                              className="text-xs"
                              text={interaction.answer_text}
                            />
                          ) : (
                            <p className="text-text-tertiary">
                              {interaction.status === "pending"
                                ? "等待补充回答…"
                                : "无回答内容"}
                            </p>
                          )}
                        </div>
                      </li>
                    ))}
                  </ol>
                </div>
              ) : null}

              {toolCalls.length === 0 && inputInteractions.length === 0 ? (
                <p className="text-text-tertiary">
                  该协作任务暂无执行步骤或问答记录。
                </p>
              ) : toolCalls.length > 0 ? (
                <div className="space-y-1.5">
                  <h4 className="text-[11px] font-semibold text-text-secondary">
                    执行步骤
                  </h4>
                  <ol className="space-y-1.5">
                    {toolCalls.map((call) => (
                      <ToolStepRow
                        call={call}
                        key={call.id}
                        runId={task.sub_run_id}
                        sessionId={sessionId}
                      />
                    ))}
                  </ol>
                </div>
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </li>
  );
}

export interface ExecutionProgressAccordionProps {
  run: Run | null;
  sessionId: string;
  status: RunStatus | null;
  subagentTasks: SubagentTaskSummary[];
}

/**
 * 去工程化的消息内折叠进度条（取代旧 344px 右侧裸 JSON 轨迹栏与独立子智能体面板）：
 * - 运行态：品牌绿轻脉冲指示点 + 业务化当前步骤文案；
 * - 完成态：自动收起为单行紧凑摘要（步骤数 · 工具业务名 · 耗时），按需展开时间线与协作任务；
 * - 所有步骤详情与子智能体详情均按需懒加载，技能工具严格坚守 SafeSkillProjection 安全门禁。
 */
export function ExecutionProgressAccordion({
  run,
  sessionId,
  status,
  subagentTasks,
}: ExecutionProgressAccordionProps) {
  const isActive = isActiveRunStatus(status);
  const isTerminal = isTerminalRunStatus(status);

  const callsQuery = useRunToolCalls(sessionId, run?.id ?? null, Boolean(run?.id));
  const toolCalls = useMemo(() => {
    const flattened = (callsQuery.data?.pages ?? []).flatMap(
      (page) => page.items,
    );
    return Array.from(
      new Map(flattened.map((item) => [item.ordinal, item])).values(),
    ).sort((left, right) => left.ordinal - right.ordinal);
  }, [callsQuery.data?.pages]);

  const approvalDisplayNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const approval of run?.pending_approvals ?? []) {
      if (approval.display_name?.trim()) {
        map.set(approval.call_id, approval.display_name.trim());
      }
    }
    return map;
  }, [run?.pending_approvals]);

  const hasSteps = toolCalls.length > 0 || subagentTasks.length > 0;
  // 活跃协作态默认展开以便实时查看子任务；已完成历史任务默认收起为单行紧凑摘要。
  const [open, setOpen] = useState(
    () => isActive && subagentTasks.length > 0,
  );
  const prevStatusRef = useRef<RunStatus | null>(status);
  const prevRunIdRef = useRef<string | null>(run?.id ?? null);

  useEffect(() => {
    const prevRunId = prevRunIdRef.current;
    const prevStatus = prevStatusRef.current;
    prevRunIdRef.current = run?.id ?? null;
    prevStatusRef.current = status;

    // 切换到新 Run 时重置折叠态；活跃任务进入终态时自动收起为单行紧凑摘要。
    if (prevRunId !== (run?.id ?? null)) {
      setOpen(isActive && subagentTasks.length > 0);
      return;
    }
    if (isActiveRunStatus(prevStatus) && isTerminalRunStatus(status)) {
      setOpen(false);
    }
  }, [isActive, run?.id, status, subagentTasks.length]);

  useEffect(() => {
    // 运行中首次派发出子智能体任务时自动展开进度条，便于员工及时查看协作状态。
    if (isActive && subagentTasks.length > 0) {
      setOpen(true);
    }
  }, [isActive, subagentTasks.length]);

  // 无 Run 或非活跃且无任何工具/协作步骤时静默隐藏，保持普通纯文本问答的极致简洁。
  if (!run || !status || (!isActive && !hasSteps)) {
    return null;
  }

  const stepNames = toolCalls.map((call) =>
    resolveToolDisplayName(call, approvalDisplayNames.get(call.id)),
  );
  const activeCall = [...toolCalls]
    .reverse()
    .find((call) => !isTerminalToolStatus(call.status));

  let summaryText: string;
  if (isActive) {
    if (status === "cancelling") {
      summaryText = "正在停止当前任务…";
    } else if (
      status === "waiting_for_approval" ||
      (run.pending_subagent_approvals?.length ?? 0) > 0
    ) {
      const firstApproval =
        run.pending_approvals[0] ?? run.pending_subagent_approvals?.[0];
      const approvalName = firstApproval
        ? firstApproval.display_name?.trim() ||
          resolveToolDisplayName({
            display_name: firstApproval.display_name,
            model_name: firstApproval.model_name,
            tool_id: null,
          })
        : null;
      summaryText = approvalName
        ? `等待确认操作「${approvalName}」`
        : "等待确认操作…";
    } else if (
      status === "waiting_for_input" ||
      (run.pending_subagent_inputs?.length ?? 0) > 0
    ) {
      summaryText = "等待补充信息…";
    } else if (status === "waiting_for_subagents" || subagentTasks.length > 0) {
      summaryText = `正在协调 ${Math.max(subagentTasks.length, 1)} 个协作任务…`;
    } else if (activeCall) {
      const activeName = resolveToolDisplayName(
        activeCall,
        approvalDisplayNames.get(activeCall.id),
      );
      summaryText =
        activeName === "查阅企业知识库"
          ? "正在查阅企业知识库…"
          : `正在调用「${activeName}」…`;
    } else if (status === "queued") {
      summaryText = "任务排队中，即将开始处理…";
    } else if (toolCalls.length > 0) {
      summaryText = `已执行 ${toolCalls.length} 个步骤，正在整理回复…`;
    } else {
      summaryText = "正在思考与生成回复…";
    }
  } else {
    const totalSteps = toolCalls.length + subagentTasks.length;
    const previewNames: string[] = [];
    for (const name of stepNames) {
      if (previewNames.length < 3 && !previewNames.includes(name)) {
        previewNames.push(name);
      }
    }
    if (subagentTasks.length > 0 && previewNames.length < 3) {
      previewNames.push(`${subagentTasks.length} 个协作任务`);
    }
    const durationText = formatDurationMs(computeRunDurationMs(run, toolCalls));
    const prefix =
      status === "completed"
        ? `已完成 ${totalSteps} 个步骤`
        : status === "failed"
          ? `已执行 ${totalSteps} 个步骤（中途失败）`
          : `已执行 ${totalSteps} 个步骤（已取消）`;
    summaryText = [
      prefix,
      previewNames.length > 0 ? previewNames.join("、") : null,
      durationText,
    ]
      .filter(Boolean)
      .join(" · ");
  }

  const canExpand = hasSteps || callsQuery.isError;

  return (
    <section
      aria-label="执行进度"
      className="rounded-xl border border-border-subtle bg-subtle/60"
    >
      <Collapsible onOpenChange={setOpen} open={canExpand ? open : false}>
        <CollapsibleTrigger asChild>
          <button
            aria-expanded={canExpand ? open : undefined}
            className={cn(
              "flex w-full items-center justify-between gap-3 rounded-xl px-3.5 py-2.5 text-left transition-colors",
              canExpand
                ? "hover:bg-subtle focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
                : "cursor-default",
            )}
            data-compact-control="true"
            disabled={!canExpand}
            type="button"
          >
            <span className="flex min-w-0 flex-1 items-center gap-2.5">
              {isActive ? (
                <span
                  aria-hidden="true"
                  className={cn(
                    "h-2 w-2 shrink-0 rounded-full animate-pulse motion-reduce:animate-none",
                    status === "waiting_for_approval"
                      ? "bg-warning"
                      : status === "waiting_for_input"
                        ? "bg-info"
                        : "bg-brand-hover",
                  )}
                />
              ) : status === "completed" ? (
                <CheckCircle2
                  aria-hidden="true"
                  className="h-4 w-4 shrink-0 text-success"
                />
              ) : status === "failed" ? (
                <AlertCircle
                  aria-hidden="true"
                  className="h-4 w-4 shrink-0 text-danger"
                />
              ) : (
                <XCircle
                  aria-hidden="true"
                  className="h-4 w-4 shrink-0 text-text-tertiary"
                />
              )}

              <span className="truncate text-xs font-medium text-text-secondary">
                {summaryText}
              </span>
            </span>

            {canExpand ? (
              <span className="inline-flex shrink-0 items-center gap-1 text-[11px] text-text-tertiary">
                <span>{open ? "收起" : "展开"}</span>
                <ChevronDown
                  aria-hidden="true"
                  className={cn(
                    "h-3.5 w-3.5 transition-transform duration-150",
                    open && "rotate-180",
                  )}
                />
              </span>
            ) : isActive && !isTerminal ? (
              <Loader2
                aria-hidden="true"
                className="h-3.5 w-3.5 shrink-0 animate-spin text-text-tertiary motion-reduce:animate-none"
              />
            ) : null}
          </button>
        </CollapsibleTrigger>

        {canExpand ? (
          <CollapsibleContent>
            <div className="space-y-3 border-t border-border-subtle px-3.5 py-3">
              {callsQuery.isError ? (
                <InlineAlert
                  action={
                    <Button
                      onClick={() => void callsQuery.refetch()}
                      size="xs"
                      variant="outline"
                    >
                      <RefreshCw aria-hidden="true" className="h-3 w-3" />
                      重试
                    </Button>
                  }
                  description="执行步骤加载失败，请重试。"
                  variant="danger"
                />
              ) : null}

              {toolCalls.length > 0 ? (
                <ol aria-label="工具调用步骤" className="space-y-1.5">
                  {toolCalls.map((call) => (
                    <ToolStepRow
                      approvalDisplayName={approvalDisplayNames.get(call.id)}
                      call={call}
                      key={call.id}
                      runId={run.id}
                      sessionId={sessionId}
                    />
                  ))}
                </ol>
              ) : null}

              {callsQuery.hasNextPage ? (
                <Button
                  className="w-full"
                  disabled={callsQuery.isFetchingNextPage}
                  onClick={() => void callsQuery.fetchNextPage()}
                  size="xs"
                  variant="ghost"
                >
                  {callsQuery.isFetchingNextPage
                    ? "正在加载更多步骤…"
                    : "加载更多步骤"}
                </Button>
              ) : null}

              {subagentTasks.length > 0 ? (
                <div className="space-y-2">
                  <div className="flex items-center gap-1.5 text-[11px] font-semibold text-text-secondary">
                    <Layers aria-hidden="true" className="h-3.5 w-3.5" />
                    <span>协作任务（{subagentTasks.length}）</span>
                  </div>
                  <ul aria-label="协作任务列表" className="space-y-2">
                    {subagentTasks.map((task) => (
                      <NestedSubagentTaskRow
                        key={task.sub_run_id}
                        sessionId={sessionId}
                        task={task}
                      />
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          </CollapsibleContent>
        ) : null}
      </Collapsible>
    </section>
  );
}
