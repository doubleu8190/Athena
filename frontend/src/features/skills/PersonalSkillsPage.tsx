import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

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
  usePublicSkills,
  useSetPersonalSkillEnabledMutation,
} from "./hooks";
import {
  skillExecutionLabel,
  skillScope,
  type Skill,
  type SkillAdminDetails,
} from "./model";
import { PersonalSkillEditorDialog } from "./PersonalSkillEditorDialog";
import { SkillScopeBadge } from "./SkillScopeBadge";

type EditorState =
  | { mode: "create"; open: true }
  | { mode: "edit"; open: true; skillId: string }
  | { mode: "closed"; open: false };

type SkillsLibraryViewName = "available" | "mine";
type CreateAction = () => void;

const CLOSED_EDITOR: EditorState = { mode: "closed", open: false };

function conflictMessage(error: unknown, fallback: string): string {
  return isSkillRevisionConflict(error)
    ? "技能已在另一个页面更新。当前操作未覆盖最新内容，请刷新列表后重试。"
    : skillErrorMessage(error) || fallback;
}

function AvailableSkillCard({ skill }: { skill: Skill }) {
  const scope = skillScope(skill);
  return (
    <li className="available-skill-card">
      <div className="available-skill-heading">
        <div className="personal-skill-title-line">
          <h3>{skill.name}</h3>
          <SkillScopeBadge scope={scope} />
          <span className="available-skill-status">后续对话可用</span>
        </div>
        <p>{skill.description || "未填写描述"}</p>
      </div>
      <dl className="available-skill-meta">
        <div>
          <dt>来源</dt>
          <dd>
            <SkillScopeBadge compact scope={scope} />
            <span>{scope === "personal" ? "本人创建" : "管理员发布"}</span>
          </dd>
        </div>
        <div>
          <dt>当前版本</dt>
          <dd>{skill.current_version_label?.trim() || "暂无版本"}</dd>
        </div>
        <div>
          <dt>执行语义</dt>
          <dd>{skillExecutionLabel(skill)}</dd>
        </div>
      </dl>
    </li>
  );
}

export function PersonalSkillsPage() {
  const [searchParams] = useSearchParams();
  const view: SkillsLibraryViewName =
    searchParams.get("view") === "mine" ? "mine" : "available";
  const createActionRef = useRef<CreateAction | null>(null);
  const availableLinkRef = useRef<HTMLAnchorElement>(null);
  const mineLinkRef = useRef<HTMLAnchorElement>(null);
  const previousViewRef = useRef(view);

  const registerCreateAction = useCallback(
    (action: CreateAction | null) => {
      createActionRef.current = action;
    },
    [],
  );

  useEffect(() => {
    if (previousViewRef.current === view) {
      return;
    }
    // AppShell 只监听 pathname，技能库的 view query 变化不会自动聚焦主内容；
    // 因而在 query 切换或浏览器前进/后退后，主动聚焦新的当前视图入口。
    (view === "mine" ? mineLinkRef : availableLinkRef).current?.focus();
    previousViewRef.current = view;
  }, [view]);

  return (
    <div className="page-shell personal-skills-page">
      <header className="page-heading">
        <div className="page-heading-copy">
          <p className="page-eyebrow">Workspace / Skill library</p>
          <h1>技能库</h1>
          <p className="page-description">
            公共技能由管理员发布，个人技能仅本人可见。启用表示允许后续对话按任务使用，并不等于每次对话都会执行。
          </p>
        </div>
        <button
          className="button-primary"
          onClick={() => createActionRef.current?.()}
          type="button"
        >
          <Icon name="plus" size={14} />
          新建个人技能
        </button>
      </header>

      <nav aria-label="技能库视图" className="skills-library-view-nav">
        <Link
          aria-current={view === "available" ? "page" : undefined}
          className={`skills-library-view-link${
            view === "available" ? " is-active" : ""
          }`}
          ref={availableLinkRef}
          to="/skills"
        >
          <Icon name="spark" size={15} />
          可用技能
        </Link>
        <Link
          aria-current={view === "mine" ? "page" : undefined}
          className={`skills-library-view-link${
            view === "mine" ? " is-active" : ""
          }`}
          ref={mineLinkRef}
          to="/skills?view=mine"
        >
          <Icon name="user" size={15} />
          我的技能
        </Link>
      </nav>

      {/* key 只隔离视图数据、提示、弹窗和 mutation；页面标题与视图导航保持在父层，避免切换时焦点被卸载。 */}
      <SkillsLibraryView
        key={view}
        registerCreateAction={registerCreateAction}
        view={view}
      />
    </div>
  );
}

interface SkillsLibraryViewProps {
  registerCreateAction: (action: CreateAction | null) => void;
  view: SkillsLibraryViewName;
}

function SkillsLibraryView({
  registerCreateAction,
  view,
}: SkillsLibraryViewProps) {
  const publicSkills = usePublicSkills(view === "available");
  const personalSkills = usePersonalSkills(view === "mine");
  const activeSkills =
    view === "mine" ? personalSkills : publicSkills;
  const toggleMutation = useSetPersonalSkillEnabledMutation();
  const deleteMutation = useDeletePersonalSkillMutation();
  const [editor, setEditor] = useState<EditorState>(CLOSED_EDITOR);
  const [deleteTarget, setDeleteTarget] = useState<Skill | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  const openCreateEditor = useCallback(() => {
    setActionError(null);
    setSuccessMessage(null);
    setEditor({ mode: "create", open: true });
  }, []);

  useEffect(() => {
    registerCreateAction(openCreateEditor);
    return () => registerCreateAction(null);
  }, [openCreateEditor, registerCreateAction]);

  const handleSaved = (
    details: SkillAdminDetails,
    mode: "create" | "edit",
  ) => {
    setEditor(CLOSED_EDITOR);
    setActionError(null);
    setSuccessMessage(
      mode === "create"
        ? `已创建并启用「${details.name}」。后续对话可按任务使用；不会自动加入当前对话。`
        : `已保存「${details.name}」的新版本并启用。后续对话可按任务使用；不会自动加入当前对话。`,
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
          ? `已启用「${details.name}」；后续对话可按任务使用，每次是否执行仍由 Agent 判断。`
          : `已停用「${details.name}」，后续对话不会再把该技能列入可用目录。`,
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
    <>
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
          onRetry={() => void activeSkills.refetch()}
          retryLabel="刷新列表"
        />
      ) : null}

      {activeSkills.isPending ? (
        <div className="page-loading" role="status">
          {view === "mine" ? "正在读取我的技能…" : "正在读取可用技能…"}
        </div>
      ) : activeSkills.isError ? (
        <ErrorBanner
          message={skillErrorMessage(activeSkills.error)}
          onRetry={() => void activeSkills.refetch()}
        />
      ) : view === "available" ? (
        activeSkills.data.length === 0 ? (
          <EmptyState
            action={
              <button
                className="button-primary"
                onClick={openCreateEditor}
                type="button"
              >
                <Icon name="plus" size={14} />
                创建个人技能
              </button>
            }
            description="管理员发布公共技能，或你在“我的技能”中启用个人技能后，会出现在这里。当前对话仍可继续。"
            icon="spark"
            kicker="Available skills"
            title="当前没有可用技能"
          />
        ) : (
          <section
            aria-labelledby="available-skill-list-title"
            className="personal-skill-section"
          >
            <header className="personal-skill-list-header">
              <div>
                <span className="empty-panel-kicker">Available catalog</span>
                <h2 id="available-skill-list-title">可用技能</h2>
              </div>
              <span>共 {activeSkills.data.length} 个</span>
            </header>
            <p className="skills-library-availability-note">
              <Icon name="shield" size={15} />
              这里展示公共技能与你已启用的个人技能；可用只表示后续对话可以按任务使用。
            </p>
            <ul className="available-skill-list">
              {activeSkills.data.map((skill) => (
                // 可用技能是运行目录投影，仅供查看；个人启停与删除只在“我的技能”中操作。
                <AvailableSkillCard key={skill.id} skill={skill} />
              ))}
            </ul>
          </section>
        )
      ) : activeSkills.data.length === 0 ? (
        <EmptyState
          action={
            <button
              className="button-primary"
              onClick={openCreateEditor}
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
              <h2 id="personal-skill-list-title">我的技能</h2>
            </div>
            <span>共 {activeSkills.data.length} 个</span>
          </header>

          <ul className="personal-skill-list">
            {activeSkills.data.map((skill) => {
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
            后续对话将不再使用该技能，进行中的任务和历史记录不受影响。删除后，当前技能目录会立即刷新。
          </p>
        </div>
        {deleteError ? (
          <ErrorBanner
            message={deleteError}
            onRetry={() => {
              void activeSkills.refetch();
              setDeleteTarget(null);
            }}
            retryLabel="刷新列表"
          />
        ) : null}
      </Modal>
    </>
  );
}
