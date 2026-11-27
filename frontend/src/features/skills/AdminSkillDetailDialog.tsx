import { FilePenLine, Sparkles, Trash2 } from "lucide-react";
import { useId } from "react";

import {
  Badge,
  Button,
  CHECK_GRID_CLASS,
  CHECKBOX_INPUT_CLASS,
  CHECKBOX_LABEL_CLASS,
  CONFIRM_COPY_CLASS,
  FIELD_CLASS,
  InlineAlert,
  MODAL_WIDTH_DETAIL,
  MODAL_WIDTH_XL,
  NOTICE_SUCCESS_CLASS,
  NOTICE_WARNING_CLASS,
  PAGE_LOADING_CLASS,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  TEXT_BUTTON_CLASS,
  cn,
} from "../../shared/ui";
import { Modal } from "../../shared/ui/Modal";
import { VisibilityGrantsEditor } from "../access/VisibilityGrantsEditor";
import {
  AdminSkillVersionPanel,
  DETAIL_PANEL_CLASS,
  DETAIL_PANEL_HEADER_CLASS,
  DependencyBadge,
  VersionUpload,
} from "./AdminSkillVersionPanel";
import { skillErrorMessage } from "./errorMessages";
import {
  SKILL_STARTER_PROMPT_MAX_LENGTH,
  type SkillAdminDetails,
} from "./model";
import { SkillScopeBadge } from "./SkillScopeBadge";
import { useAdminSkillDetailState } from "./useAdminSkillDetailState";

interface AdminSkillDetailDialogProps {
  onClose: () => void;
  /** 由父页面安全关闭旧 <Modal> 详情窗后受控切换打开 Radix 在线发版 Dialog，严禁在旧 Modal 上叠开低层 Dialog。 */
  onEditMarkdownVersion?: (skill: SkillAdminDetails) => void;
  open: boolean;
  skillId: string | null;
}

/** 公共/部门技能详情与发布配置弹窗（薄装配壳，状态由 useAdminSkillDetailState 托管） */
export function AdminSkillDetailDialog({
  onClose,
  onEditMarkdownVersion,
  open,
  skillId,
}: AdminSkillDetailDialogProps) {
  const versionSelectId = useId();
  const featuredSwitchId = useId();
  const starterPromptId = useId();
  const starterPromptErrorId = useId();

  const state = useAdminSkillDetailState({ onClose, open, skillId });
  const {
    baseline,
    canEnable,
    close,
    conflictMessage,
    currentDepartmentName,
    currentVersionDetail,
    currentVersionOutsideKnownList,
    deleteDialogOpen,
    deleteError,
    deleting,
    detail,
    form,
    handleConfirmDeleteDepartmentSkill,
    hasRevisionConflict,
    isDepartmentSkill,
    isDirty,
    isMarkdownSkill,
    isVersionDirty,
    notice,
    pending,
    reloadLatestConfiguration,
    requestError,
    save,
    saving,
    selectedVersion,
    selectedVersionDetail,
    selectedVersionNeedsDetail,
    selectedVersionOptionLabel,
    setDeleteDialogOpen,
    setDeleteError,
    setForm,
    setNotice,
    setRequestError,
    setStarterPromptError,
    setVersionPageNumber,
    setVersionSelectOpen,
    starterPromptError,
    starterPromptLength,
    starterPromptOverLimit,
    superAdmin,
    versionDetailPending,
    versionOptions,
    versionPage,
    versionPageNumber,
    versionSelectOpen,
    versions,
  } = state;

  return (
    <Modal
      closeDisabled={pending}
      description="版本内容不可修改；当前版本、启停和脚本审批在同一修订号下保存。"
      footer={
        <>
          {isDepartmentSkill ? (
            <Button
              disabled={pending || hasRevisionConflict}
              onClick={() => {
                setDeleteError(null);
                setDeleteDialogOpen(true);
              }}
              type="button"
              variant="danger-outline"
            >
              <Trash2
                aria-hidden="true"
                focusable="false"
                size={14}
                strokeWidth={1.7}
              />
              删除部门技能
            </Button>
          ) : null}
          <Button
            disabled={pending}
            onClick={close}
            type="button"
            variant="outline"
          >
            关闭
          </Button>
          <Button
            disabled={
              pending ||
              !form ||
              !selectedVersion ||
              hasRevisionConflict
            }
            onClick={() => void save()}
            type="button"
            variant="primary"
          >
            {saving ? "正在保存配置…" : "保存当前配置"}
          </Button>
        </>
      }
      onClose={close}
      open={open}
      panelClassName={MODAL_WIDTH_DETAIL}
      title={
        detail.data ? `技能：${detail.data.name}` : "技能详情"
      }
    >
      {detail.isPending && !detail.data ? (
        <div className={PAGE_LOADING_CLASS} role="status">
          正在读取技能版本与检查结果…
        </div>
      ) : detail.isError && !detail.data ? (
        <InlineAlert
          action={
            <button
              className={TEXT_BUTTON_CLASS}
              onClick={() => void detail.refetch()}
              type="button"
            >
              重试
            </button>
          }
          size="compact"
          variant="danger"
        >
          {skillErrorMessage(detail.error)}
        </InlineAlert>
      ) : detail.data && form ? (
        <div className="flex min-w-0 flex-col gap-[18px]">
          <section
            aria-labelledby="skill-config-title"
            className={DETAIL_PANEL_CLASS}
          >
            <header className={DETAIL_PANEL_HEADER_CLASS}>
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h3 id="skill-config-title">当前发布配置</h3>
                  <SkillScopeBadge
                    departmentName={
                      isDepartmentSkill ? currentDepartmentName : null
                    }
                    scope={detail.data.scope}
                  />
                  <Badge variant={isMarkdownSkill ? "info" : "neutral"}>
                    {isMarkdownSkill ? "在线模板" : "脚本包"}
                  </Badge>
                  {detail.data.featured ? (
                    <Badge variant="brand">
                      <Sparkles aria-hidden="true" className="h-3 w-3" />
                      首页推荐
                    </Badge>
                  ) : null}
                </div>
                <p>
                  revision {detail.data.revision} · slug{" "}
                  <code>{detail.data.slug}</code>
                </p>
              </div>
              <DependencyBadge
                check={
                  selectedVersion?.dependency_check ??
                  detail.data.dependency_check
                }
              />
            </header>

            {hasRevisionConflict ? (
              <div
                className={cn(
                  NOTICE_WARNING_CLASS,
                  "mb-3 flex flex-col items-stretch justify-between gap-3 font-medium min-[721px]:flex-row min-[721px]:items-center",
                )}
                role="alert"
              >
                <p>
                  {conflictMessage ??
                    `服务端配置已更新到 revision ${detail.data.revision}，当前${isDirty ? "草稿" : "编辑基线"}仍保留在 revision ${baseline?.revision ?? "未知"}，保存已锁定。`}
                </p>
                <Button
                  disabled={pending}
                  onClick={() => void reloadLatestConfiguration()}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  重新载入服务端最新配置
                </Button>
              </div>
            ) : null}

            {/* 桌面端采用左右双栏网格：左侧版本选择，右侧启停与审批开关 */}
            <div className="grid min-w-0 grid-cols-1 items-start gap-x-4 gap-y-3 min-[721px]:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
              <div className={FIELD_CLASS}>
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[13px] font-semibold text-text-primary">
                    当前版本
                  </span>
                  {isVersionDirty ? (
                    <Badge className="rounded-full" variant="brand">
                      未保存变更
                    </Badge>
                  ) : null}
                </div>
                {/* 使用统一 Radix Select，并在展开态向旧 Modal 声明 Escape 归属权以避免误关外层详情弹窗 */}
                <Select
                  disabled={pending}
                  onOpenChange={(nextOpen) => {
                    if (!pending) {
                      setVersionSelectOpen(nextOpen);
                    }
                  }}
                  onValueChange={(nextVersionId) => {
                    setForm((current) =>
                      current
                        ? {
                            ...current,
                            currentVersionId: nextVersionId,
                          }
                        : current,
                    );
                    setRequestError(null);
                    setNotice(null);
                  }}
                  open={versionSelectOpen}
                  value={form.currentVersionId}
                >
                  <SelectTrigger
                    aria-label="当前版本"
                    className="min-w-0 text-left"
                    // 下拉刚展开、Popper 尚未将焦点切入列表项的瞬间，确保触发器自身也阻止旧 Modal 抢先消费 Escape。
                    data-modal-escape-owner={
                      versionSelectOpen ? "self" : undefined
                    }
                    id={versionSelectId}
                  >
                    {/* 仅在触发器内展示纯版本号文案，避免把下拉项中的“依赖未满足”徽章带入触发器导致布局拥挤 */}
                    <SelectValue placeholder="请选择版本">
                      {selectedVersionOptionLabel}
                    </SelectValue>
                  </SelectTrigger>
                  <SelectContent aria-label="当前版本列表">
                    {versionOptions.map((option) => (
                      <SelectItem
                        className="[&>span:first-child]:flex [&>span:first-child]:min-w-0 [&>span:first-child]:flex-1 [&>span:first-child]:items-center [&>span:first-child]:justify-between [&>span:first-child]:gap-2"
                        key={option.value}
                        textValue={option.label}
                        value={option.value}
                      >
                        <span className="min-w-0 flex-1 truncate">
                          {option.label}
                        </span>
                        {option.badge ? (
                          <span className="shrink-0 rounded-full border border-warning-border bg-warning-bg px-1.5 py-px text-xs font-semibold whitespace-nowrap text-warning">
                            {option.badge}
                          </span>
                        ) : null}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <small>
                  保存后只影响新 Run；已创建 Run 始终使用创建时固定版本。
                </small>
              </div>

              <div
                className={cn(
                  CHECK_GRID_CLASS,
                  "flex-col gap-2.5 min-[721px]:mt-[26px]",
                )}
              >
                <label className={CHECKBOX_LABEL_CLASS}>
                  <input
                    checked={form.enabled}
                    className={CHECKBOX_INPUT_CLASS}
                    // 已启用但当前环境依赖失效时仍必须允许管理员关闭技能。
                    disabled={pending || (!canEnable && !form.enabled)}
                    onChange={(event) => {
                      setForm({
                        ...form,
                        enabled: event.target.checked,
                      });
                      setRequestError(null);
                    }}
                    type="checkbox"
                  />
                  <span>向普通用户启用该技能</span>
                </label>
                <label className={CHECKBOX_LABEL_CLASS}>
                  <input
                    checked={form.scriptRequiresApproval}
                    className={CHECKBOX_INPUT_CLASS}
                    disabled={pending}
                    onChange={(event) =>
                      setForm({
                        ...form,
                        scriptRequiresApproval: event.target.checked,
                      })
                    }
                    type="checkbox"
                  />
                  <span>每次脚本执行均需用户审批</span>
                </label>
              </div>
            </div>

            {/* 工作台首页快捷应用推荐与默认引导语配置（纳入同一 expected_revision CAS 事务保存） */}
            <div className="mt-4 space-y-3 rounded-xl border border-border-default bg-subtle/40 p-3.5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="space-y-0.5">
                  <label
                    className="cursor-pointer text-xs font-semibold text-text-primary select-none"
                    htmlFor={featuredSwitchId}
                  >
                    推荐至工作台首页
                  </label>
                  <p className="text-xs text-text-tertiary">
                    开启后将在有权限成员的对话首页「常用办公场景与快捷应用」区域优先展示。
                  </p>
                </div>
                <Switch
                  aria-label="推荐至工作台首页"
                  checked={form.featured}
                  disabled={pending}
                  id={featuredSwitchId}
                  onCheckedChange={(checked) => {
                    setForm((current) =>
                      current ? { ...current, featured: checked } : current,
                    );
                    setRequestError(null);
                    setNotice(null);
                  }}
                  size="sm"
                />
              </div>

              <div className="space-y-1.5">
                <div className="flex items-center justify-between gap-2">
                  <label
                    className="text-xs font-semibold text-text-primary"
                    htmlFor={starterPromptId}
                  >
                    首页默认引导语
                  </label>
                  <span
                    className={cn(
                      "text-xs tabular-nums",
                      starterPromptOverLimit
                        ? "font-semibold text-danger"
                        : "text-text-tertiary",
                    )}
                  >
                    {starterPromptLength} / {SKILL_STARTER_PROMPT_MAX_LENGTH} 字符
                  </span>
                </div>
                <textarea
                  aria-describedby={
                    starterPromptError ? starterPromptErrorId : undefined
                  }
                  aria-invalid={
                    starterPromptError || starterPromptOverLimit
                      ? true
                      : undefined
                  }
                  className={cn(
                    "block w-full rounded-lg border bg-canvas px-3 py-2 text-sm text-text-primary shadow-xs transition-colors",
                    "placeholder:text-text-placeholder focus-visible:outline-hidden focus-visible:ring-2",
                    "disabled:cursor-not-allowed disabled:bg-subtle disabled:text-text-tertiary",
                    starterPromptError || starterPromptOverLimit
                      ? "border-danger focus-visible:border-danger focus-visible:ring-danger/20"
                      : "border-border-default hover:border-border-strong focus-visible:border-ring-focus focus-visible:ring-ring-focus/20",
                  )}
                  disabled={pending}
                  id={starterPromptId}
                  onChange={(event) => {
                    setForm((current) =>
                      current
                        ? { ...current, starterPrompt: event.target.value }
                        : current,
                    );
                    setStarterPromptError(null);
                    setRequestError(null);
                    setNotice(null);
                  }}
                  placeholder="用户点击首页快捷应用卡片且输入框为空时自动填入；清空保存即可清除引导语"
                  rows={2}
                  value={form.starterPrompt}
                />
                {starterPromptError ? (
                  <p
                    className="text-xs font-medium text-danger"
                    id={starterPromptErrorId}
                    role="alert"
                  >
                    {starterPromptError}
                  </p>
                ) : null}
              </div>
            </div>

            {isDepartmentSkill && detail.data.owner_department_id ? (
              <VisibilityGrantsEditor
                className="mt-4"
                defaultIncludeSubdepartments={superAdmin}
                disabled={pending}
                enabled={open && isDepartmentSkill}
                onChange={(nextGrants) => {
                  setForm((current) =>
                    current
                      ? { ...current, visibilityGrants: nextGrants }
                      : current,
                  );
                  setRequestError(null);
                  setNotice(null);
                }}
                ownerDepartmentId={detail.data.owner_department_id}
                ownerDepartmentName={currentDepartmentName}
                resourceLabel="技能"
                value={form.visibilityGrants}
              />
            ) : null}
            {!canEnable ? (
              <p className={cn(NOTICE_WARNING_CLASS, "mt-3 font-semibold")}>
                所选版本依赖未满足，后端将拒绝启用；可切换至检查通过的版本，或由部署管理员补齐统一 Python 环境。
              </p>
            ) : null}
            {currentVersionOutsideKnownList &&
            currentVersionDetail.isError ? (
              <div className="mt-3">
                <InlineAlert
                  action={
                    <button
                      className={TEXT_BUTTON_CLASS}
                      onClick={() => void currentVersionDetail.refetch()}
                      type="button"
                    >
                      重试
                    </button>
                  }
                  size="compact"
                  variant="danger"
                >
                  当前版本详情加载失败，暂时不能保存或预览该版本。
                </InlineAlert>
              </div>
            ) : null}
            {selectedVersionNeedsDetail &&
            selectedVersionDetail.isError ? (
              <div className="mt-3">
                <InlineAlert
                  action={
                    <button
                      className={TEXT_BUTTON_CLASS}
                      onClick={() => void selectedVersionDetail.refetch()}
                      type="button"
                    >
                      重试
                    </button>
                  }
                  size="compact"
                  variant="danger"
                >
                  所选版本详情加载失败，暂时不能保存或预览该版本。
                </InlineAlert>
              </div>
            ) : null}
            {requestError ? (
              <div className="mt-3">
                <InlineAlert size="compact" variant="danger">
                  {requestError}
                </InlineAlert>
              </div>
            ) : null}
            {notice ? (
              <p className={cn(NOTICE_SUCCESS_CLASS, "mt-3")} role="status">
                {notice}
              </p>
            ) : null}
          </section>

          {/* 严格执行双向格式锁 UI：在线模板仅展示 Markdown 发版入口并隐藏 ZIP 上传；ZIP 包仅保留 ZIP 上传 */}
          {isMarkdownSkill ? (
            <section
              aria-labelledby="skill-markdown-version-title"
              className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-border-default bg-canvas p-4"
              data-testid="markdown-version-publish-section"
            >
              <div className="min-w-0 flex-1 space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <h4
                    className="text-sm font-semibold text-text-primary"
                    id="skill-markdown-version-title"
                  >
                    在线模板版本编辑
                  </h4>
                  <Badge variant="info">在线 Markdown 形态</Badge>
                </div>
                <p className="text-xs leading-relaxed text-text-secondary">
                  本技能受双向格式锁保护（不支持通过 ZIP 覆盖），可直接在双栏 Markdown 编辑器中修改指南并追加不可变新版本。
                </p>
              </div>
              <Button
                disabled={pending || hasRevisionConflict || !onEditMarkdownVersion}
                onClick={() => {
                  if (detail.data) {
                    onEditMarkdownVersion?.(detail.data);
                  }
                }}
                size="sm"
                type="button"
                variant="primary"
              >
                <FilePenLine aria-hidden="true" className="h-3.5 w-3.5" />
                在线编辑并发布新版本
              </Button>
            </section>
          ) : (
            <VersionUpload
              disabled={pending}
              onUploaded={() => setVersionPageNumber(1)}
              skillId={detail.data.id}
              slug={detail.data.slug}
            />
          )}

          <AdminSkillVersionPanel
            currentVersionId={form.currentVersionId}
            disabled={pending}
            onPageChange={setVersionPageNumber}
            onRetryPage={() => void versionPage.refetch()}
            onSelectVersion={(versionId) => {
              setForm({
                ...form,
                currentVersionId: versionId,
              });
              setRequestError(null);
              setNotice(null);
            }}
            pageError={versionPage.isError}
            pageFetching={versionPage.isFetching}
            pageNumber={versionPageNumber}
            pagePending={versionPage.isPending}
            pageResultPage={versionPage.data?.page}
            pageTotal={versionPage.data?.total}
            selectedVersion={selectedVersion}
            versionDetailPending={versionDetailPending}
            versions={versions}
          />
        </div>
      ) : null}

      {/* 复用支持 ModalDepthContext 层级栈的嵌套子 Modal，确保确认框稳定位于详情弹窗之上且 Escape/焦点隔离正常。 */}
      {isDepartmentSkill && detail.data ? (
        <Modal
          closeDisabled={deleting}
          description="删除后后续新对话将不再展示该技能，历史运行记录不受影响。"
          footer={
            <>
              <Button
                disabled={deleting}
                onClick={() => setDeleteDialogOpen(false)}
                type="button"
                variant="outline"
              >
                取消
              </Button>
              <Button
                disabled={deleting}
                onClick={() => void handleConfirmDeleteDepartmentSkill()}
                type="button"
                variant="danger"
              >
                {deleting ? "正在删除…" : "确认删除"}
              </Button>
            </>
          }
          onClose={() => {
            if (!deleting) {
              setDeleteDialogOpen(false);
            }
          }}
          open={deleteDialogOpen}
          panelClassName={MODAL_WIDTH_XL}
          title="删除部门技能"
        >
          <p className={CONFIRM_COPY_CLASS}>
            确定软删除部门技能「{detail.data.name}」（slug:{" "}
            <code>{detail.data.slug}</code>）吗？
          </p>
          {deleteError ? (
            <div className="mt-3">
              <InlineAlert description={deleteError} variant="danger" />
            </div>
          ) : null}
        </Modal>
      ) : null}
    </Modal>
  );
}
