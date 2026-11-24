import { ShieldAlert } from "lucide-react";

import { formatBytes } from "../../shared/lib/format";
import { Badge } from "../../shared/ui";
import { SessionFileDownloadButton } from "../files/SessionFileDownloadButton";
import type { SafeSkillApprovalProjection } from "./model";

interface SafeSkillProjectionProps {
  showParameters?: boolean;
  projection: SafeSkillApprovalProjection;
  sessionId: string;
}

/**
 * 技能脚本安全投影视图：
 * 严格仅展示后端脱敏与校验后的安全投影字段（技能名、版本、脚本、用途、授权文件与可见参数摘要），
 * 绝不接触或渲染原始脚本命令、隐藏参数或内部路径。
 */
export function SafeSkillProjection({
  showParameters = true,
  projection,
  sessionId,
}: SafeSkillProjectionProps) {
  return (
    <div className="mt-3 space-y-3 rounded-lg border border-border-default bg-canvas p-3.5 text-xs">
      <dl className="grid grid-cols-1 gap-x-5 gap-y-2 sm:grid-cols-2">
        <div className="min-w-0 space-y-0.5">
          <dt className="text-[11px] font-medium text-text-tertiary">技能</dt>
          <dd className="flex flex-wrap items-center gap-1.5 font-medium text-text-primary">
            <span>{projection.skill_name}</span>
            <Badge variant="brand">{projection.skill_version_label}</Badge>
          </dd>
        </div>
        <div className="min-w-0 space-y-0.5">
          <dt className="text-[11px] font-medium text-text-tertiary">脚本</dt>
          <dd className="font-medium text-text-primary">
            {projection.script_label}
          </dd>
        </div>
        <div className="min-w-0 space-y-0.5 sm:col-span-2">
          <dt className="text-[11px] font-medium text-text-tertiary">
            执行用途
          </dt>
          <dd className="leading-relaxed text-text-secondary">
            {projection.purpose}
          </dd>
        </div>
      </dl>

      <div className="space-y-1.5 border-t border-border-subtle pt-2.5">
        <div className="text-[11px] font-semibold text-text-secondary">
          输入文件
        </div>
        {projection.input_files.length === 0 ? (
          <p className="text-text-tertiary">本次执行不读取会话文件。</p>
        ) : (
          <ul className="space-y-1.5">
            {projection.input_files.map((file) => (
              <li
                className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border-subtle bg-subtle/60 px-2.5 py-1.5"
                key={file.file_id}
              >
                <span className="min-w-0 flex-1">
                  <strong className="block truncate font-medium text-text-primary">
                    {file.filename}
                  </strong>
                  <small className="block truncate text-[11px] text-text-tertiary">
                    {file.mime_type ?? "未知类型"}
                    {file.size_bytes === null
                      ? ""
                      : ` · ${formatBytes(file.size_bytes)}`}
                    {file.available ? "" : " · 当前不可访问"}
                  </small>
                </span>
                {file.available ? (
                  <SessionFileDownloadButton
                    compact
                    fileId={file.file_id}
                    filename={file.filename}
                    sessionId={sessionId}
                  />
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>

      {showParameters ? (
        <div className="space-y-1.5 border-t border-border-subtle pt-2.5">
          <div className="text-[11px] font-semibold text-text-secondary">
            可展示参数
          </div>
          {projection.parameter_summary.length === 0 ? (
            <p className="text-text-tertiary">本次调用没有可展示的参数摘要。</p>
          ) : (
            <dl className="grid grid-cols-1 gap-x-5 gap-y-1.5 sm:grid-cols-2">
              {projection.parameter_summary.map((item) => (
                <div
                  className="flex min-w-0 items-baseline justify-between gap-2 rounded-md bg-subtle/70 px-2.5 py-1.5"
                  key={item.name}
                >
                  <dt className="shrink-0 text-text-secondary">{item.label}</dt>
                  <dd className="min-w-0 break-words text-right font-medium text-text-primary">
                    {item.value}
                  </dd>
                </div>
              ))}
            </dl>
          )}
          {projection.hidden_parameter_count > 0 ? (
            <p className="text-[11px] text-text-tertiary">
              另有 {projection.hidden_parameter_count}{" "}
              个内部参数未向普通用户展示。
            </p>
          ) : null}
        </div>
      ) : null}

      {!projection.can_approve ? (
        <div
          className="flex items-start gap-2 rounded-lg border border-danger-border bg-danger-bg px-3 py-2 text-xs text-danger"
          role="note"
        >
          <ShieldAlert aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            {projection.unavailable_reason ??
              "审批所需安全元数据缺失或已失效，不能批准；仍可拒绝本次调用。"}
          </span>
        </div>
      ) : null}
    </div>
  );
}
