import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import {
  Badge,
  Button,
  InlineAlert,
  PAGE_DESC_CLASS,
  PAGE_HEADING_CLASS,
  PAGE_HEADING_COPY_CLASS,
  PAGE_LOADING_CLASS,
  PAGE_SHELL_CLASS,
  PAGE_TITLE_CLASS,
  Tabs,
  TabsList,
  TabsTrigger,
  cn,
} from "../../shared/ui";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { Modal } from "../../shared/ui/Modal";
import { useAuth } from "../auth/AuthProvider";
import { resolveUserDepartmentName } from "../auth/model";
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
  type SkillScope,
} from "./model";
import { PersonalSkillEditorDialog } from "./PersonalSkillEditorDialog";
import { SkillScopeBadge } from "./SkillScopeBadge";

type EditorState =
  | { mode: "create"; open: true }
  | { mode: "edit"; open: true; skillId: string }
  | { mode: "closed"; open: false };

type SkillsLibraryViewName = "available" | "mine";
type AvailableScopeFilter = "all" | SkillScope;
type CreateAction = () => void;

const CLOSED_EDITOR: EditorState = { mode: "closed", open: false };

const AVAILABLE_SCOPE_FILTERS: ReadonlyArray<{
  label: string;
  value: AvailableScopeFilter;
}> = [
  { label: "全部", value: "all" },
  { label: "全公司", value: "company" },
  { label: "部门共享", value: "department" },
  { label: "个人", value: "personal" },
];

function conflictMessage(error: unknown, fallback: string): string {
  return isSkillRevisionConflict(error)
    ? "技能已在另一个页面更新。当前操作未覆盖最新内容，请刷新列表后重试。"
    : skillErrorMessage(error) || fallback;
}

function skillSourceCopy(scope: SkillScope): string {
  switch (scope) {
    case "company":
      return "全公司发布";
    case "department":
      return "部门发布";
    case "personal":
      return "本人创建";
  }
}

const SKILL_GRID_CLASS =
  "m-0 grid list-none grid-cols-1 gap-[13px] p-0 min-[821px]:grid-cols-2";
const SKILL_CARD_CLASS =
  "flex min-w-0 flex-col gap-3.5 rounded-xl border border-border-default bg-canvas p-4 shadow-xs sm:px-[19px] sm:py-[18px]";
const SKILL_LIST_HEADER_CLASS =
  "mb-3 flex flex-col items-start justify-between gap-1.5 border-b border-border-subtle px-0.5 pb-3 sm:flex-row sm:items-end sm:gap-[18px] [&_h2]:mt-[3px] [&_h2]:text-lg [&_h2]:font-semibold [&_h2]:text-text-primary";
const SKILL_TITLE_LINE_CLASS =
  "flex min-w-0 flex-wrap items-center gap-[7px] [&_h3]:min-w-0 [&_h3]:text-base [&_h3]:font-semibold [&_h3]:text-text-primary [&_h3]:[overflow-wrap:anywhere]";

function AvailableSkillCard({
  departmentName,
  skill,
}: {
  departmentName: string | null;
  skill: Skill;
}) {
  const scope = skillScope(skill);
  return (
    <li className={SKILL_CARD_CLASS}>
      <div className="min-w-0">
        <div className={SKILL_TITLE_LINE_CLASS}>
          <h3>{skill.name}</h3>
          <SkillScopeBadge departmentName={departmentName} scope={scope} />
          <Badge className="rounded-full" variant="success">
            后续对话可用
          </Badge>
        </div>
        <p className="mt-2 text-[13px] leading-[1.65] text-text-secondary [overflow-wrap:anywhere]">
          {skill.description || "未填写描述"}
        </p>
      </div>
      <dl className="mt-auto grid grid-cols-1 gap-2 border-t border-dashed border-border-default pt-3 sm:grid-cols-3 sm:gap-[9px] [&>div]:min-w-0 [&_dt]:text-[11px] [&_dt]:font-semibold [&_dt]:text-text-tertiary [&_dd]:mt-1 [&_dd]:flex [&_dd]:min-w-0 [&_dd]:flex-wrap [&_dd]:items-center [&_dd]:gap-[5px] [&_dd]:text-xs [&_dd]:leading-[1.5] [&_dd]:text-text-primary [&_dd]:[overflow-wrap:anywhere]">
        <div>
          <dt>来源</dt>
          <dd>
            <SkillScopeBadge
              compact
              departmentName={departmentName}
              scope={scope}
            />
            <span>{skillSourceCopy(scope)}</span>
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
  const [searchParams, setSearchParams] = useSearchParams();
  const view: SkillsLibraryViewName =
    searchParams.get("view") === "mine" ? "mine" : "available";
  const createActionRef = useRef<CreateAction | null>(null);
  const availableTabRef = useRef<HTMLButtonElement>(null);
  const mineTabRef = useRef<HTMLButtonElement>(null);
  const previousViewRef = useRef(view);
  // 记录最近一次已提交的目标视图，避免 Radix Tabs 在单次点击的 mousedown + focus 中重复 push 历史记录。
  const latestViewRef = useRef(view);

  const registerCreateAction = useCallback(
    (action: CreateAction | null) => {
      createActionRef.current = action;
    },
    [],
  );

  useEffect(() => {
    latestViewRef.current = view;
    if (previousViewRef.current === view) {
      return;
    }
    // 浏览器前进/后退或 query 切换视图后，主动聚焦新的当前标签触发器，维持键盘可达性。
    (view === "mine" ? mineTabRef : availableTabRef).current?.focus();
    previousViewRef.current = view;
  }, [view]);

  const handleViewChange = (nextValue: string) => {
    const nextView: SkillsLibraryViewName =
      nextValue === "mine" ? "mine" : "available";
    if (nextView === latestViewRef.current) {
      return;
    }
    latestViewRef.current = nextView;
    setSearchParams(nextView === "mine" ? { view: "mine" } : {});
  };

  return (
    <div className={cn(PAGE_SHELL_CLASS, "max-w-[1160px]")}>
      <header className={PAGE_HEADING_CLASS}>
        <div className={PAGE_HEADING_COPY_CLASS}>
          <h1 className={PAGE_TITLE_CLASS}>技能库</h1>
          <p className={PAGE_DESC_CLASS}>
            企业与部门技能由对应管理员发布，个人技能仅本人可见。启用表示允许后续对话按任务使用，并不等于每次对话都会执行。
          </p>
        </div>
        <Button
          onClick={() => createActionRef.current?.()}
          type="button"
          variant="primary"
        >
          <Icon name="plus" size={14} />
          新建个人技能
        </Button>
      </header>

      <Tabs onValueChange={handleViewChange} value={view} variant="underline">
        <TabsList aria-label="技能库视图">
          <TabsTrigger ref={availableTabRef} value="available">
            <Icon name="spark" size={15} />
            可用技能
          </TabsTrigger>
          <TabsTrigger ref={mineTabRef} value="mine">
            <Icon name="user" size={15} />
            我的技能
          </TabsTrigger>
        </TabsList>
      </Tabs>

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
  const { user } = useAuth();
  const publicSkills = usePublicSkills(view === "available");
  const personalSkills = usePersonalSkills(view === "mine");
  const activeSkills =
    view === "mine" ? personalSkills : publicSkills;
  const toggleMutation = useSetPersonalSkillEnabledMutation();
  const deleteMutation = useDeletePersonalSkillMutation();
  const [scopeFilter, setScopeFilter] = useState<AvailableScopeFilter>("all");
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

  const filteredAvailableSkills = useMemo(() => {
    const items = publicSkills.data ?? [];
    if (scopeFilter === "all") {
      return items;
    }
    return items.filter((skill) => skillScope(skill) === scopeFilter);
  }, [publicSkills.data, scopeFilter]);

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
        <InlineAlert
          aria-live="polite"
          className="mb-3.5 text-[13px]"
          variant="success"
        >
          {successMessage}
        </InlineAlert>
      ) : null}
      {actionError ? (
        <ErrorBanner
          message={actionError}
          onRetry={() => void activeSkills.refetch()}
          retryLabel="刷新列表"
        />
      ) : null}

      {activeSkills.isPending ? (
        <div className={PAGE_LOADING_CLASS} role="status">
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
              <Button onClick={openCreateEditor} type="button" variant="primary">
                <Icon name="plus" size={14} />
                创建个人技能
              </Button>
            }
            description="管理员发布全公司或部门技能，或你在“我的技能”中启用个人技能后，会出现在这里。当前对话仍可继续。"
            icon="spark"
            title="当前没有可用技能"
          />
        ) : (
          <section
            aria-labelledby="available-skill-list-title"
            className="mt-2"
          >
            <header className={SKILL_LIST_HEADER_CLASS}>
              <div>
                <h2 id="available-skill-list-title">可用技能</h2>
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <Tabs
                  onValueChange={(value) =>
                    setScopeFilter(value as AvailableScopeFilter)
                  }
                  value={scopeFilter}
                  variant="segmented"
                >
                  <TabsList aria-label="按来源筛选可用技能">
                    {AVAILABLE_SCOPE_FILTERS.map((option) => (
                      <TabsTrigger key={option.value} value={option.value}>
                        {option.label}
                      </TabsTrigger>
                    ))}
                  </TabsList>
                </Tabs>
                <span className="shrink-0 text-xs text-text-tertiary">
                  共 {filteredAvailableSkills.length} 个
                </span>
              </div>
            </header>
            <p className="-mt-0.5 mb-3 flex items-start gap-[7px] text-xs leading-[1.65] text-text-tertiary [&>svg]:mt-px [&>svg]:shrink-0 [&>svg]:text-brand-hover">
              <Icon name="shield" size={15} />
              这里展示全公司技能、所属部门共享技能与你已启用的个人技能；可用只表示后续对话可以按任务使用。
            </p>
            {filteredAvailableSkills.length === 0 ? (
              <EmptyState
                action={
                  <Button
                    onClick={() => setScopeFilter("all")}
                    type="button"
                    variant="outline"
                  >
                    查看全部可用技能
                  </Button>
                }
                compact
                description="当前筛选条件下没有可用技能，可切换回「全部」查看。"
                icon="spark"
                title="该范围内暂无可用技能"
              />
            ) : (
              <ul className={SKILL_GRID_CLASS}>
                {filteredAvailableSkills.map((skill) => (
                  // 可用技能是运行目录投影，仅供查看；个人启停与删除只在“我的技能”中操作。
                  <AvailableSkillCard
                    departmentName={resolveUserDepartmentName(
                      user,
                      skill.owner_department_id,
                    )}
                    key={skill.id}
                    skill={skill}
                  />
                ))}
              </ul>
            )}
          </section>
        )
      ) : activeSkills.data.length === 0 ? (
        <EmptyState
          action={
            <Button onClick={openCreateEditor} type="button" variant="primary">
              <Icon name="plus" size={14} />
              新建第一个个人技能
            </Button>
          }
          description="在线编写方法，或导入一个 Markdown 文件。保存后仅你本人可以使用。"
          icon="spark"
          title="还没有个人技能"
        />
      ) : (
        <section aria-labelledby="personal-skill-list-title" className="mt-2">
          <header className={SKILL_LIST_HEADER_CLASS}>
            <div>
              <h2 id="personal-skill-list-title">我的技能</h2>
            </div>
            <span className="shrink-0 text-xs text-text-tertiary">
              共 {activeSkills.data.length} 个
            </span>
          </header>

          <ul className={SKILL_GRID_CLASS}>
            {activeSkills.data.map((skill) => {
              const toggling =
                toggleMutation.isPending &&
                toggleMutation.variables?.skillId === skill.id;
              return (
                <li className={SKILL_CARD_CLASS} key={skill.id}>
                  <div className="min-w-0">
                    <div className={SKILL_TITLE_LINE_CLASS}>
                      <h3>{skill.name}</h3>
                      <SkillScopeBadge scope="personal" />
                      <Badge
                        className="rounded-full"
                        variant={skill.enabled ? "success" : "neutral"}
                      >
                        {skill.enabled ? "已启用" : "已停用"}
                      </Badge>
                    </div>
                    <p className="mt-2 text-[13px] leading-[1.65] text-text-secondary [overflow-wrap:anywhere]">
                      {skill.description || "未填写描述"}
                    </p>
                  </div>

                  <dl className="m-0 grid grid-cols-1 gap-2 border-y border-dashed border-border-default py-3 sm:grid-cols-2 sm:gap-2.5 [&>div]:min-w-0 [&_dt]:text-[11px] [&_dt]:font-semibold [&_dt]:text-text-tertiary [&_dd]:mt-1 [&_dd]:text-xs [&_dd]:leading-[1.5] [&_dd]:text-text-primary [&_dd]:[overflow-wrap:anywhere]">
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

                  <div className="mt-auto flex min-w-0 flex-wrap gap-2 [&>button]:flex-[1_1_100%] sm:[&>button]:flex-[1_1_94px]">
                    <Button
                      aria-label={`编辑个人技能：${skill.name}`}
                      onClick={() => {
                        setActionError(null);
                        setSuccessMessage(null);
                        setEditor({
                          mode: "edit",
                          open: true,
                          skillId: skill.id,
                        });
                      }}
                      size="sm"
                      type="button"
                      variant="outline"
                    >
                      <Icon name="edit" size={14} />
                      编辑
                    </Button>
                    <Button
                      aria-label={`${skill.enabled ? "停用" : "启用"}个人技能：${skill.name}`}
                      disabled={toggling}
                      onClick={() => void handleToggle(skill)}
                      size="sm"
                      type="button"
                      variant="outline"
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
                    </Button>
                    <Button
                      aria-label={`删除个人技能：${skill.name}`}
                      onClick={() => {
                        setDeleteError(null);
                        setDeleteTarget(skill);
                      }}
                      size="sm"
                      type="button"
                      variant="danger-outline"
                    >
                      <Icon name="trash" size={14} />
                      删除
                    </Button>
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
            <Button
              disabled={deleteMutation.isPending}
              onClick={() => setDeleteTarget(null)}
              type="button"
              variant="outline"
            >
              取消
            </Button>
            <Button
              disabled={deleteMutation.isPending}
              onClick={() => void handleDelete()}
              type="button"
              variant="danger"
            >
              {deleteMutation.isPending ? "正在删除…" : "确认删除"}
            </Button>
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
        <div className="flex items-start gap-2.5 text-[13px] leading-[1.7] text-text-secondary [&>svg]:mt-0.5 [&>svg]:shrink-0 [&>svg]:text-danger">
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
