import { FilePenLine, Sparkles } from "lucide-react";
import { useRef, useState } from "react";

import { formatDateTime } from "../../shared/lib/format";
import { Badge } from "../../shared/ui";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { useAuth } from "../auth/AuthProvider";
import {
  isDeptAdmin,
  resolveUserDepartmentName,
} from "../auth/model";
import { AdminSkillDetailDialog } from "./AdminSkillDetailDialog";
import { AdminSkillMarkdownDialog } from "./AdminSkillMarkdownDialog";
import { skillErrorMessage } from "./errorMessages";
import { useAdminSkills } from "./hooks";
import {
  dependencySatisfied,
  skillFormat,
  skillScope,
  type SkillAdminDetails,
} from "./model";
import { SkillPackageDialog } from "./SkillPackageDialog";
import { SkillScopeBadge } from "./SkillScopeBadge";

export function AdminSkillsPage() {
  const { user } = useAuth();
  const skills = useAdminSkills();
  const [importOpen, setImportOpen] = useState(false);
  const [createMarkdownOpen, setCreateMarkdownOpen] = useState(false);
  const [selectedSkillId, setSelectedSkillId] = useState<string | null>(null);
  // 与 selectedSkillId 互斥切换，严禁在旧 <Modal> 详情窗上方叠开 Radix Dialog。
  const [editingVersionSkill, setEditingVersionSkill] =
    useState<SkillAdminDetails | null>(null);
  // 记录进入详情窗的列表按钮，便于从旧 <Modal> 切入在线发版 Dialog 后取消/Escape 时精准归还焦点。
  const lastDetailTriggerRef = useRef<HTMLElement | null>(null);
  const deptAdmin = isDeptAdmin(user);

  return (
    <div className="page-shell admin-page admin-skills-page">
      <header className="page-heading">
        <div className="page-heading-copy">
          <h1>{deptAdmin ? "部门技能管理" : "公共技能"}</h1>
          <p className="page-description">
            {deptAdmin
              ? "在线撰写部门 Markdown 模板技能或导入脚本技能包，配置跨部门共享、首页快捷推荐、当前版本与启停策略，并支持按需删除部门技能。"
              : "在线撰写全公司与各部门 Markdown 模板技能或导入可信脚本技能包，统一管理首页快捷推荐、跨部门共享、版本历史与脚本审批策略。"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            className="button-quiet"
            onClick={() => setImportOpen(true)}
            type="button"
          >
            <Icon name="upload" size={14} />
            导入技能 ZIP
          </button>
          <button
            className="button-primary"
            onClick={() => setCreateMarkdownOpen(true)}
            type="button"
          >
            <FilePenLine aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
            新建在线模板技能
          </button>
        </div>
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
            <div className="flex flex-wrap items-center justify-center gap-2">
              <button
                className="button-primary"
                onClick={() => setCreateMarkdownOpen(true)}
                type="button"
              >
                新建在线模板技能
              </button>
              <button
                className="button-quiet"
                onClick={() => setImportOpen(true)}
                type="button"
              >
                导入第一个技能
              </button>
            </div>
          }
          description="可直接在线编写纯 Markdown 模板技能（支持导入 .md 与双栏预览），或导入包含 Python 脚本的完整 ZIP 技能包。"
          icon="spark"
          title="还没有技能包"
        />
      ) : (
        <section
          aria-labelledby="admin-skill-list-title"
          className="admin-section"
        >
          <header className="admin-section-heading">
            <div>
              <h2 id="admin-skill-list-title">技能目录</h2>
            </div>
            <span className="admin-data-count">
              后端返回 {skills.data.length} 个技能
            </span>
          </header>

          <ul className="admin-skill-list">
            {skills.data.map((skill) => {
              const scope = skillScope(skill);
              const format = skillFormat(skill);
              const departmentName =
                resolveUserDepartmentName(user, skill.owner_department_id) ??
                skill.owner_department_id ??
                null;
              const starterPromptSummary = skill.starter_prompt?.trim() ?? "";

              return (
                <li className="admin-skill-card" key={skill.id}>
                  <div className="admin-card-heading">
                    <div className="min-w-0">
                      <div className="admin-title-line">
                        <h3>{skill.name}</h3>
                        <SkillScopeBadge
                          departmentName={
                            scope === "department" ? departmentName : null
                          }
                          scope={scope}
                        />
                        <Badge
                          variant={format === "markdown" ? "info" : "neutral"}
                        >
                          {format === "markdown" ? "在线模板" : "脚本包"}
                        </Badge>
                        {skill.featured ? (
                          <Badge variant="brand">
                            <Sparkles
                              aria-hidden="true"
                              className="h-3 w-3 shrink-0"
                            />
                            首页推荐
                          </Badge>
                        ) : null}
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
                      {starterPromptSummary ? (
                        <p className="mt-1.5 line-clamp-2 text-xs leading-relaxed text-text-secondary">
                          <span className="font-medium text-brand">
                            首页引导语：
                          </span>
                          {starterPromptSummary}
                        </p>
                      ) : null}
                    </div>
                    <button
                      className="button-quiet"
                      onClick={(event) => {
                        lastDetailTriggerRef.current = event.currentTarget;
                        setSelectedSkillId(skill.id);
                      }}
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
                        {format === "markdown"
                          ? "纯文档（无脚本）"
                          : skill.script_requires_approval
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
                      <dd className="admin-code-value">{skill.id}</dd>
                    </div>
                  </dl>
                </li>
              );
            })}
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

      <AdminSkillMarkdownDialog
        mode="create"
        onClose={() => setCreateMarkdownOpen(false)}
        onSaved={(created) => {
          setCreateMarkdownOpen(false);
          setSelectedSkillId(created.id);
          void skills.refetch();
        }}
        open={createMarkdownOpen}
      />

      <AdminSkillMarkdownDialog
        mode="version"
        onClose={() => setEditingVersionSkill(null)}
        onSaved={(updated) => {
          setEditingVersionSkill(null);
          setSelectedSkillId(updated.id);
          void skills.refetch();
        }}
        open={Boolean(editingVersionSkill)}
        returnFocusRef={lastDetailTriggerRef}
        skill={editingVersionSkill}
      />

      <AdminSkillDetailDialog
        onClose={() => {
          setSelectedSkillId(null);
          void skills.refetch();
        }}
        onEditMarkdownVersion={(skillDetails) => {
          // 先彻底关闭旧 <Modal> 详情窗，再受控打开 Radix 在线发版 Dialog，保证零弹窗层级冲突。
          setSelectedSkillId(null);
          setEditingVersionSkill(skillDetails);
        }}
        open={Boolean(selectedSkillId)}
        skillId={selectedSkillId}
      />
    </div>
  );
}
