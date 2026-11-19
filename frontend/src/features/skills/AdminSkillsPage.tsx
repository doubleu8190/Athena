import { useState } from "react";

import { formatDateTime } from "../../shared/lib/format";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { AdminSkillDetailDialog } from "./AdminSkillDetailDialog";
import { skillErrorMessage } from "./errorMessages";
import { useAdminSkills } from "./hooks";
import { dependencySatisfied } from "./model";
import { SkillPackageDialog } from "./SkillPackageDialog";

export function AdminSkillsPage() {
  const skills = useAdminSkills();
  const [importOpen, setImportOpen] = useState(false);
  const [selectedSkillId, setSelectedSkillId] = useState<string | null>(
    null,
  );

  return (
    <div className="page-shell admin-page admin-skills-page">
      <header className="page-heading">
        <div className="page-heading-copy">
          <p className="page-eyebrow">Admin / Skills</p>
          <h1>公共技能</h1>
          <p className="page-description">
            发布和管理所有用户可在后续对话中按任务使用的公共技能；导入可信技能包，检查依赖，查看不可变指南与资源版本，再决定当前版本、启停和脚本审批。
          </p>
        </div>
        <button
          className="button-primary"
          onClick={() => setImportOpen(true)}
          type="button"
        >
          <Icon name="upload" size={14} />
          导入技能 ZIP
        </button>
      </header>

      {skills.isPending ? (
        <div className="page-loading" role="status">
          正在读取技能目录…
        </div>
      ) : skills.isError ? (
        <ErrorBanner
          message={skillErrorMessage(skills.error)}
          onRetry={() => void skills.refetch()}
        />
      ) : skills.data.length === 0 ? (
        <EmptyState
          action={
            <button
              className="button-primary"
              onClick={() => setImportOpen(true)}
              type="button"
            >
              导入第一个技能
            </button>
          }
          description="首发导入会完整校验 ZIP，成功后才创建不可变版本；首次导入保持未启用。"
          icon="spark"
          kicker="No installed skills"
          title="还没有技能包"
        />
      ) : (
        <section
          aria-labelledby="admin-skill-list-title"
          className="admin-section"
        >
          <header className="admin-section-heading">
            <div>
              <p className="admin-section-kicker">Installed packages</p>
              <h2 id="admin-skill-list-title">技能目录</h2>
            </div>
            <span className="admin-data-count">
              后端返回 {skills.data.length} 个技能
            </span>
          </header>

          <ul className="admin-skill-list">
            {skills.data.map((skill) => (
              <li className="admin-skill-card" key={skill.id}>
                <div className="admin-card-heading">
                  <div>
                    <div className="admin-title-line">
                      <h3>{skill.name}</h3>
                      <span
                        className={`admin-status-badge ${
                          skill.enabled ? " is-success" : " is-muted"
                        }`}
                      >
                        {skill.enabled ? "已启用" : "未启用"}
                      </span>
                      <span className="admin-status-badge">
                        revision {skill.revision}
                      </span>
                    </div>
                    <p className="admin-code-line">
                      {skill.slug} · {skill.description}
                    </p>
                  </div>
                  <button
                    className="button-quiet"
                    onClick={() => setSelectedSkillId(skill.id)}
                    type="button"
                  >
                    <Icon name="eye" size={14} />
                    查看版本与指南
                  </button>
                </div>

                <dl className="admin-detail-grid">
                  <div>
                    <dt>当前版本</dt>
                    <dd>
                    {skill.current_version_label?.trim() ||
                      skill.current_version_id ||
                      "未选择"}
                    </dd>
                  </div>
                  <div>
                    <dt>脚本审批</dt>
                    <dd>
                      {skill.script_requires_approval
                        ? "每次执行需审批"
                        : "默认无需审批"}
                    </dd>
                  </div>
                  <div>
                    <dt>依赖检查</dt>
                    <dd>
                      {dependencySatisfied(skill.dependency_check)
                        ? "满足当前目标 Python 环境"
                        : "未满足或尚未取得有效检查结果"}
                    </dd>
                  </div>
                  <div>
                    <dt>目录修订号</dt>
                    <dd>{skill.revision}</dd>
                  </div>
                  <div>
                    <dt>更新时间</dt>
                    <dd>{formatDateTime(skill.updated_at)}</dd>
                  </div>
                  <div>
                    <dt>技能 ID</dt>
                    <dd className="admin-code-value">
                      {skill.id}
                    </dd>
                  </div>
                </dl>
              </li>
            ))}
          </ul>
        </section>
      )}

      <SkillPackageDialog
        onClose={() => setImportOpen(false)}
        onImported={(skillId) => {
          setImportOpen(false);
          setSelectedSkillId(skillId);
          void skills.refetch();
        }}
        open={importOpen}
      />
      <AdminSkillDetailDialog
        onClose={() => {
          setSelectedSkillId(null);
          void skills.refetch();
        }}
        open={Boolean(selectedSkillId)}
        skillId={selectedSkillId}
      />
    </div>
  );
}
