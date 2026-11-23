import { useState } from "react";

import { formatDateTime } from "../../shared/lib/format";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { MarkdownContent } from "../../shared/ui/MarkdownContent";
import { useRunDetails, useRunToolCalls } from "./hooks";
import type { SubagentTaskSummary } from "./model";
import { RunStatusBadge } from "./RunStatusBadge";

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

function SubagentTaskCard({
  sessionId,
  task,
}: {
  sessionId: string;
  task: SubagentTaskSummary;
}) {
  const [expanded, setExpanded] = useState(false);
  // 仅在用户展开子任务卡片时按需拉取子 Run 详情与工具调用轨迹。
  const detailQuery = useRunDetails(sessionId, task.sub_run_id, expanded);
  const toolCallsQuery = useRunToolCalls(sessionId, task.sub_run_id, expanded);
  const toolCalls = (toolCallsQuery.data?.pages ?? []).flatMap(
    (page) => page.items,
  );
  const inputInteractions = detailQuery.data?.input_interactions ?? [];

  return (
    <li className="subagent-task-card">
      <div className="subagent-task-header">
        <div className="subagent-task-identity">
          <div className="subagent-task-title">
            <span className="subagent-task-index">#{task.task_index}</span>
            <strong>{task.subagent_name}</strong>
            <code className="subagent-task-slug">{task.subagent_slug}</code>
            <RunStatusBadge status={task.status} />
          </div>
          {task.task_summary ? (
            <p className="subagent-task-summary">{task.task_summary}</p>
          ) : null}
          <div className="subagent-task-meta">
            <span>子任务 {task.sub_run_id}</span>
            {task.first_started_at ? (
              <span>开始 {formatDateTime(task.first_started_at)}</span>
            ) : null}
            {task.finished_at ? (
              <span>结束 {formatDateTime(task.finished_at)}</span>
            ) : null}
          </div>
          {task.safe_error_code ? (
            <p className="subagent-task-error">
              安全错误码：{task.safe_error_code}
            </p>
          ) : null}
        </div>
        <button
          aria-expanded={expanded}
          className="button-quiet"
          onClick={() => setExpanded((value) => !value)}
          type="button"
        >
          <Icon
            className="subagent-task-toggle-chevron"
            name="chevron"
            size={13}
          />
          {expanded ? "收起详情" : "查看详情"}
        </button>
      </div>

      {expanded ? (
        <div className="subagent-task-detail">
          {detailQuery.isPending || toolCallsQuery.isPending ? (
            <p className="subagent-task-loading" role="status">
              正在加载子任务执行轨迹…
            </p>
          ) : detailQuery.isError ? (
            <ErrorBanner
              message="子任务详情加载失败。"
              onRetry={() => void detailQuery.refetch()}
            />
          ) : toolCallsQuery.isError ? (
            <ErrorBanner
              message="子任务工具调用加载失败。"
              onRetry={() => void toolCallsQuery.refetch()}
            />
          ) : (
            <>
              {detailQuery.data ? (
                <div className="subagent-task-detail-meta">
                  <span>事件 #{detailQuery.data.event_sequence}</span>
                  <span>执行代次 {detailQuery.data.execution_epoch}</span>
                  {detailQuery.data.safe_error_code ? (
                    <span>错误码：{detailQuery.data.safe_error_code}</span>
                  ) : null}
                </div>
              ) : null}
              {inputInteractions.length > 0 ? (
                <div className="subagent-interactions">
                  <h3 className="subagent-interactions-title">
                    人工输入记录（提问与回答）
                  </h3>
                  <ol className="subagent-interaction-list">
                    {inputInteractions.map((interaction) => (
                      <li
                        className="subagent-interaction-item"
                        key={interaction.input_request_id}
                      >
                        <div className="subagent-interaction-head">
                          <span className="subagent-interaction-status">
                            {interactionStatusLabel(interaction.status)}
                          </span>
                          <span className="subagent-interaction-time">
                            提问于 {formatDateTime(interaction.created_at)}
                            {interaction.answered_at
                              ? ` · 回答于 ${formatDateTime(interaction.answered_at)}`
                              : ""}
                          </span>
                        </div>
                        <div className="subagent-interaction-block">
                          <span className="subagent-interaction-label">提问</span>
                          <MarkdownContent text={interaction.question_text} />
                        </div>
                        <div className="subagent-interaction-block">
                          <span className="subagent-interaction-label">回答</span>
                          {interaction.answer_text ? (
                            <MarkdownContent text={interaction.answer_text} />
                          ) : (
                            <p className="subagent-task-empty">
                              {interaction.status === "pending"
                                ? "等待用户回答…"
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
                <p className="subagent-task-empty">
                  该子任务暂无工具调用或人工输入记录。
                </p>
              ) : toolCalls.length > 0 ? (
                <ol className="subagent-task-tools">
                  {toolCalls.map((call) => (
                    <li className="subagent-task-tool-row" key={call.id}>
                      <span
                        className={`trace-call-dot trace-call-dot--${call.status}`}
                      />
                      <span className="subagent-task-tool-name">
                        #{call.ordinal} {call.model_name}
                      </span>
                      <span className="subagent-task-tool-status">
                        {call.status}
                        {call.duration_ms === null
                          ? ""
                          : ` · ${call.duration_ms} ms`}
                      </span>
                      {call.safe_error_code ? (
                        <span className="subagent-task-error">
                          {call.safe_error_code}
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ol>
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </li>
  );
}

interface SubagentExecutionPanelProps {
  sessionId: string;
  tasks: SubagentTaskSummary[];
}

export function SubagentExecutionPanel({
  sessionId,
  tasks,
}: SubagentExecutionPanelProps) {
  if (tasks.length === 0) {
    return null;
  }

  return (
    <section
      aria-labelledby="subagent-execution-title"
      className="subagent-execution-panel"
    >
      <header className="subagent-panel-header">
        <div>
          <p className="empty-panel-kicker">Subagent execution</p>
          <h2 id="subagent-execution-title">
            子智能体任务（{tasks.length}）
          </h2>
        </div>
      </header>
      <ul className="subagent-task-list">
        {tasks.map((task) => (
          <SubagentTaskCard
            key={task.sub_run_id}
            sessionId={sessionId}
            task={task}
          />
        ))}
      </ul>
    </section>
  );
}
