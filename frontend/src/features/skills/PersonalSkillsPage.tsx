import { useState } from "react";

import { formatDateTime } from "../../shared/lib/format";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { Modal } from "../../shared/ui/Modal";
import {
  isSkillRevisionConflict,
  skillErrorMessage,
} from "./errorMessages";
import {
  useDeletePersonalSkillMutation,
  usePersonalSkills,
  useSetPersonalSkillEnabledMutation,
} from "./hooks";
import type { Skill, SkillAdminDetails } from "./model";
import { PersonalSkillEditorDialog } from "./PersonalSkillEditorDialog";
import { SkillScopeBadge } from "./SkillScopeBadge";

type EditorState =
  | { mode: "create"; open: true }
  | { mode: "edit"; open: true; skillId: string }
  | { mode: "closed"; open: false };

const CLOSED_EDITOR: EditorState = { mode: "closed", open: false };

function conflictMessage(error: unknown, fallback: string): string {
  return isSkillRevisionConflict(error)
    ? "技能已在另一个页面更新。当前操作未覆盖最新内容，请刷新列表后重试。"
    : skillErrorMessage(error) || fallback;
}

export function PersonalSkillsPage() {
  const skills = usePersonalSkills();
  const toggleMutation = useSetPersonalSkillEnabledMutation();
  const deleteMutation = useDeletePersonalSkillMutation();
  const [editor, setEditor] = useState<EditorState>(CLOSED_EDITOR);
  const [deleteTarget, setDeleteTarget] = useState<Skill | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  const handleSaved = (
    details: SkillAdminDetails,
    mode: "create" | "edit",
  ) => {
    setEditor(CLOSED_EDITOR);
    setActionError(null);
    setSuccessMessage(
      mode === "create"
        ? `已创建并启用「${details.name}」。下一次新任务可以使用该技能。`
        : `已保存「${details.name}」的新版本并启用。`,
    );
  };

  const handleToggle = async (skill: Skill) => {
    setActionError(null);
    setSuccessMessage(null);
    try {
      const details = await toggleMutation.mutateAsync({
        enabled: !skill.enabled,
        expectedRevision: skill.revision,
        skillId: skill.id,
      });
      setSuccessMessage(
        details.enabled
          ? `已启用「${details.name}」。`
          : `已停用「${details.name}」，新任务将不再使用该技能。`,
      );
    } catch (error) {
      setActionError(
        conflictMessage(error, "技能状态未能更新，请稍后重试。"),
      );
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) {
      return;
    }
    setDeleteError(null);
    setActionError(null);
    setSuccessMessage(null);
    try {
      await deleteMutation.mutateAsync({
        expectedRevision: deleteTarget.revision,
        skillId: deleteTarget.id,
      });
      setSuccessMessage(
        `已删除「${deleteTarget.name}」。进行中的任务和历史记录不受影响。`,
      );
      setDeleteTarget(null);
    } catch (error) {
      setDeleteError(
        conflictMessage(error, "技能未能删除，请稍后重试。"),
      );
    }
  };

  return (
    <div className="page-shell personal-skills-page">
      <header className="page-heading">
        <div className="page-heading-copy">
          <p className="page-eyebrow">Workspace / Personal Skills</p>
          <h1>我的技能</h1>
          <p className="page-description">
            用 Markdown 保存只对自己可见的工作方法。保存会启用新版本，你也可以随时停用或删除。
          </p>
        </div>
        <button
          className="button-primary"
          onClick={() => {
            setActionError(null);
            setSuccessMessage(null);
            setEditor({ mode: "create", open: true });
          }}
          type="button"
        >
          <Icon name="plus" size={14} />
          新建个人技能
        </button>
      </header>

      {successMessage ? (
        <div
          aria-live="polite"
          className="personal-skill-success"
          role="status"
        >
          <Icon name="spark" size={16} />
          <span>{successMessage}</span>
        </div>
      ) : null}
      {actionError ? (
        <ErrorBanner
          message={actionError}
          onRetry={() => void skills.refetch()}
          retryLabel="刷新列表"
        />
      ) : null}

      {skills.isPending ? (
        <div className="page-loading" role="status">
          正在读取我的技能…
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
              onClick={() => setEditor({ mode: "create", open: true })}
              type="button"
            >
              <Icon name="plus" size={14} />
              新建第一个个人技能
            </button>
          }
          description="在线编写方法，或导入一个 Markdown 文件。保存后仅你本人可以使用。"
          icon="spark"
          kicker="Private Markdown skills"
          title="还没有个人技能"
        />
      ) : (
        <section
          aria-labelledby="personal-skill-list-title"
          className="personal-skill-section"
        >
          <header className="personal-skill-list-header">
            <div>
              <span className="empty-panel-kicker">Private catalog</span>
              <h2 id="personal-skill-list-title">个人技能目录</h2>
            </div>
            <span>共 {skills.data.length} 个</span>
          </header>

          <ul className="personal-skill-list">
            {skills.data.map((skill) => {
              const toggling =
                toggleMutation.isPending &&
                toggleMutation.variables?.skillId === skill.id;
              return (
                <li className="personal-skill-card" key={skill.id}>
                  <div className="personal-skill-card-heading">
                    <div className="personal-skill-title-line">
                      <h3>{skill.name}</h3>
                      <SkillScopeBadge scope="personal" />
                      <span
                        className={`personal-skill-status${
                          skill.enabled ? " is-enabled" : ""
                        }`}
                      >
                        {skill.enabled ? "已启用" : "已停用"}
                      </span>
                    </div>
                    <p>{skill.description || "未填写描述"}</p>
                  </div>

                  <dl className="personal-skill-meta">
                    <div>
                      <dt>当前版本</dt>
                      <dd>
                        {skill.current_version_label?.trim() ||
                          skill.current_version_id ||
                          "未选择"}
                      </dd>
                    </div>
                    <div>
                      <dt>更新时间</dt>
                      <dd>{formatDateTime(skill.updated_at)}</dd>
                    </div>
                  </dl>

                  <div className="personal-skill-actions">
                    <button
                      aria-label={`编辑个人技能：${skill.name}`}
                      className="button-quiet"
                      onClick={() => {
                        setActionError(null);
                        setSuccessMessage(null);
                        setEditor({
                          mode: "edit",
                          open: true,
                          skillId: skill.id,
                        });
                      }}
                      type="button"
                    >
                      <Icon name="edit" size={14} />
                      编辑
                    </button>
                    <button
                      aria-label={`${skill.enabled ? "停用" : "启用"}个人技能：${skill.name}`}
                      className="button-quiet"
                      disabled={toggling}
                      onClick={() => void handleToggle(skill)}
                      type="button"
                    >
                      <Icon
                        name={skill.enabled ? "eye-off" : "eye"}
                        size={14}
                      />
                      {toggling
                        ? "正在更新…"
                        : skill.enabled
                          ? "停用"
                          : "启用"}
                    </button>
                    <button
                      aria-label={`删除个人技能：${skill.name}`}
                      className="button-danger-outline"
                      onClick={() => {
                        setDeleteError(null);
                        setDeleteTarget(skill);
                      }}
                      type="button"
                    >
                      <Icon name="trash" size={14} />
                      删除
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      <PersonalSkillEditorDialog
        mode={editor.mode === "edit" ? "edit" : "create"}
        onClose={() => setEditor(CLOSED_EDITOR)}
        onSaved={handleSaved}
        open={editor.open}
        skillId={editor.mode === "edit" ? editor.skillId : undefined}
      />

      <Modal
        closeDisabled={deleteMutation.isPending}
        description="删除后不能从当前页面恢复。"
        footer={
          <>
            <button
              className="button-quiet"
              disabled={deleteMutation.isPending}
              onClick={() => setDeleteTarget(null)}
              type="button"
            >
              取消
            </button>
            <button
              className="button-danger"
              disabled={deleteMutation.isPending}
              onClick={() => void handleDelete()}
              type="button"
            >
              {deleteMutation.isPending ? "正在删除…" : "确认删除"}
            </button>
          </>
        }
        onClose={() => {
          if (!deleteMutation.isPending) {
            setDeleteTarget(null);
          }
        }}
        open={Boolean(deleteTarget)}
        title={deleteTarget ? `删除「${deleteTarget.name}」？` : "删除个人技能？"}
      >
        <div className="personal-skill-delete-copy">
          <Icon name="shield" size={18} />
          <p>
            新任务将无法使用该技能，进行中的任务和历史记录不受影响。删除后，当前技能目录会立即刷新。
          </p>
        </div>
        {deleteError ? (
          <ErrorBanner
            message={deleteError}
            onRetry={() => {
              void skills.refetch();
              setDeleteTarget(null);
            }}
            retryLabel="刷新列表"
          />
        ) : null}
      </Modal>
    </div>
  );
}
