import { formatBytes } from "../../shared/lib/format";
import { Icon } from "../../shared/ui/Icons";
import { SessionFileDownloadButton } from "../files/SessionFileDownloadButton";
import type { SafeSkillApprovalProjection } from "./model";

interface SafeSkillProjectionProps {
  showParameters?: boolean;
  projection: SafeSkillApprovalProjection;
  sessionId: string;
}

export function SafeSkillProjection({
  showParameters = true,
  projection,
  sessionId,
}: SafeSkillProjectionProps) {
  return (
    <div className="skill-approval-projection">
      <dl className="skill-approval-summary">
        <div>
          <dt>技能</dt>
          <dd>{projection.skill_name}</dd>
        </div>
        <div>
          <dt>固定版本</dt>
          <dd>{projection.skill_version_label}</dd>
        </div>
        <div>
          <dt>脚本</dt>
          <dd>{projection.script_label}</dd>
        </div>
        <div>
          <dt>执行用途</dt>
          <dd>{projection.purpose}</dd>
        </div>
      </dl>

      <div className="skill-projection-section">
        <strong>输入文件</strong>
        {projection.input_files.length === 0 ? (
          <p className="trace-empty">本次执行不读取会话文件。</p>
        ) : (
          <ul className="skill-projection-files">
            {projection.input_files.map((file) => (
              <li key={file.file_id}>
                <span>
                  <strong>{file.filename}</strong>
                  <small>
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
        <div className="skill-projection-section">
          <strong>可展示参数</strong>
          {projection.parameter_summary.length === 0 ? (
            <p className="trace-empty">本次调用没有可展示的参数摘要。</p>
          ) : (
            <ul className="skill-projection-parameters">
              {projection.parameter_summary.map((item) => (
                <li key={item.name}>
                  <span>{item.label}</span>
                  <small>{item.value}</small>
                </li>
              ))}
            </ul>
          )}
          {projection.hidden_parameter_count > 0 ? (
            <p className="admin-muted">
              另有 {projection.hidden_parameter_count} 个内部参数未向普通用户展示。
            </p>
          ) : null}
        </div>
      ) : null}

      {!projection.can_approve ? (
        <div className="skill-projection-unavailable" role="note">
          <Icon name="shield" size={15} />
          <span>
            {projection.unavailable_reason ??
              "审批所需安全元数据缺失或已失效，不能批准；仍可拒绝本次调用。"}
          </span>
        </div>
      ) : null}
    </div>
  );
}
