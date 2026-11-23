import { useState } from "react";

import { formatDateTime } from "../../shared/lib/format";
import { Icon } from "../../shared/ui/Icons";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { useRunToolCalls, useToolCallDetail } from "./hooks";
import { isSkillTool, type RunToolCallSummary } from "./model";
import { SafeSkillProjection } from "./SafeSkillProjection";
import { SessionFileDownloadButton } from "../files/SessionFileDownloadButton";
import { fileDisplayName } from "../files/model";

function ToolCallRow({
  call,
  runId,
  sessionId,
}: {
  call: RunToolCallSummary;
  runId: string;
  sessionId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const details = useToolCallDetail(
    sessionId,
    runId,
    call.id,
    expanded,
  );
  // 安全投影本身也是权威技能判别；即使模型展示名变化也不能回退裸参数。
  const skillCall =
    isSkillTool(call.model_name, call.tool_id) ||
    Boolean(details.data?.safe_skill_projection);
  const skillProjection =
    details.data?.safe_skill_projection ?? null;
  const artifacts = details.data?.artifacts ?? [];

  return (
    <li className="trace-call">
      <button
        aria-expanded={expanded}
        className="trace-call-toggle"
        onClick={() => setExpanded((value) => !value)}
        type="button"
      >
        <span className={`trace-call-dot trace-call-dot--${call.status}`} />
        <span className="trace-call-main">
          <strong>
            #{call.ordinal} {call.model_name}
          </strong>
          <span>
            {call.tool_kind ?? "未标注类型"} · {call.status}
            {call.duration_ms === null ? "" : ` · ${call.duration_ms} ms`}
          </span>
        </span>
        <Icon name="chevron" size={12} />
      </button>
      {call.safe_error_code ? (
        <p className="trace-call-error">安全错误码：{call.safe_error_code}</p>
      ) : null}
      {expanded ? (
        <div className="trace-call-detail">
          {details.isPending ? (
            <p role="status">正在读取调用详情…</p>
          ) : details.isError ? (
            <ErrorBanner
              message="工具调用详情加载失败。"
              onRetry={() => void details.refetch()}
            />
          ) : details.data && skillCall ? (
            <>
              <div className="trace-detail-meta">
                <span>
                  开始：
                  {details.data.started_at
                    ? formatDateTime(details.data.started_at)
                    : "未开始"}
                </span>
                <span>
                  结束：
                  {details.data.finished_at
                    ? formatDateTime(details.data.finished_at)
                    : "未结束"}
                </span>
              </div>
              {skillProjection ? (
                <SafeSkillProjection
                  projection={skillProjection}
                  sessionId={sessionId}
                />
              ) : (
                <div
                  className="skill-projection-unavailable"
                  role="alert"
                >
                  该技能调用缺少服务端安全投影。普通用户详情不会回退展示原始参数、输出或日志。
                </div>
              )}
              {artifacts.length > 0 ? (
                <ArtifactList
                  artifacts={artifacts}
                  sessionId={sessionId}
                />
              ) : null}
            </>
          ) : details.data ? (
            <>
              <div className="trace-detail-meta">
                <span>
                  开始：
                  {details.data.started_at
                    ? formatDateTime(details.data.started_at)
                    : "未开始"}
                </span>
                <span>
                  结束：
                  {details.data.finished_at
                    ? formatDateTime(details.data.finished_at)
                    : "未结束"}
                </span>
                {details.data.truncated ? <span>结果已截断</span> : null}
              </div>
              <div className="trace-code-block">
                <strong>arguments</strong>
                <pre>
                  {JSON.stringify(details.data.arguments ?? {}, null, 2)}
                </pre>
              </div>
              {details.data.outcome ? (
                <div className="trace-code-block">
                  <strong>
                    outcome · {details.data.outcome.status} ·{" "}
                    {details.data.outcome.code}
                  </strong>
                  <p>{details.data.outcome.message}</p>
                  <pre>{JSON.stringify(details.data.outcome.data, null, 2)}</pre>
                  {details.data.outcome.cleanup_error_code ? (
                    <span>
                      清理错误：{details.data.outcome.cleanup_error_code}
                    </span>
                  ) : null}
                </div>
              ) : (
                <p className="trace-empty">该调用尚未产生 outcome。</p>
              )}
              {artifacts.length > 0 ? (
                <ArtifactList
                  artifacts={artifacts}
                  sessionId={sessionId}
                />
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

function ArtifactList({
  artifacts,
  sessionId,
}: {
  artifacts: NonNullable<
    ReturnType<typeof useToolCallDetail>["data"]
  >["artifacts"];
  sessionId: string;
}) {
  return (
    <div className="trace-code-block">
      <strong>交付文件</strong>
      <ul className="message-artifact-list">
        {(artifacts ?? []).map((artifact) => (
          <li className="message-artifact" key={artifact.file_id}>
            <span>
              <strong>{fileDisplayName(artifact)}</strong>
              <small>
                {artifact.filename} · {artifact.mime_type} ·{" "}
                {artifact.size_bytes.toLocaleString()} B
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

interface RunExecutionTraceProps {
  onClose: () => void;
  onOpen: () => void;
  open: boolean;
  runId: string | null;
  sessionId: string;
}

export function RunExecutionTrace({
  onClose,
  onOpen,
  open,
  runId,
  sessionId,
}: RunExecutionTraceProps) {
  // 仅在执行轨迹面板展开时请求工具调用列表，收起态或窄屏默认隐藏时不发提前请求。
  const calls = useRunToolCalls(sessionId, runId, open);
  const items = (calls.data?.pages ?? []).flatMap((page) => page.items);
  const uniqueItems = Array.from(
    new Map(items.map((item) => [item.ordinal, item])).values(),
  ).sort((left, right) => left.ordinal - right.ordinal);

  return (
    <>
      {open ? (
        <button
          aria-label="关闭执行轨迹"
          className="trace-backdrop"
          onClick={onClose}
          type="button"
        />
      ) : null}
      <aside
        aria-label="执行轨迹"
        className={`run-trace-panel${open ? " is-open" : " is-collapsed"}`}
        hidden={!open}
      >
        <header className="run-trace-header">
          <div>
            <p className="page-eyebrow">Run execution</p>
            <h2>执行轨迹</h2>
          </div>
          <button
            aria-expanded={open}
            aria-label={open ? "收起执行轨迹" : "展开执行轨迹"}
            className="icon-button"
            onClick={onClose}
            type="button"
          >
            {open ? <Icon name="close" size={16} /> : <Icon name="tools" size={16} />}
          </button>
        </header>

        {open ? (
          <div className="run-trace-body">
            {!runId ? (
              <p className="trace-empty">当前会话还没有 Run。</p>
            ) : calls.isPending ? (
              <p className="trace-empty" role="status">
                正在加载工具调用…
              </p>
            ) : calls.isError ? (
              <ErrorBanner
                message="工具调用加载失败。"
                onRetry={() => void calls.refetch()}
              />
            ) : uniqueItems.length === 0 ? (
              <p className="trace-empty">该 Run 暂无工具调用记录。</p>
            ) : (
              <ol className="trace-list">
                {uniqueItems.map((call) => (
                  <ToolCallRow
                    call={call}
                    key={call.id}
                    runId={runId}
                    sessionId={sessionId}
                  />
                ))}
              </ol>
            )}
            {calls.hasNextPage ? (
              <button
                className="button-quiet trace-more"
                disabled={calls.isFetchingNextPage}
                onClick={() => void calls.fetchNextPage()}
                type="button"
              >
                {calls.isFetchingNextPage ? "正在加载…" : "加载更多工具调用"}
              </button>
            ) : null}
          </div>
        ) : null}
      </aside>
      {!open ? (
        <button
          aria-label="展开执行轨迹"
          className="run-trace-tab"
          onClick={onOpen}
          type="button"
        >
          <Icon name="tools" size={15} />
          轨迹
        </button>
      ) : null}
    </>
  );
}

