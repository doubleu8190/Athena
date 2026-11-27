import { FileCode2, Sparkles } from "lucide-react";
import { useId, type RefObject } from "react";

import {
  Badge,
  Button,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  InlineAlert,
  MarkdownSplitEditor,
} from "../../shared/ui";
import {
  AdminSkillMarkdownMetaFields,
  AdminSkillMarkdownPublishFields,
} from "./AdminSkillMarkdownMetaFields";
import {
  PERSONAL_SKILL_GUIDE_MAX_LENGTH,
  type SkillAdminDetails,
} from "./model";
import { useAdminSkillMarkdownForm } from "./useAdminSkillMarkdownForm";

export type AdminSkillMarkdownDialogProps =
  | {
      mode: "create";
      onClose: () => void;
      onSaved: (details: SkillAdminDetails) => void;
      open: boolean;
      returnFocusRef?: RefObject<HTMLElement | null>;
      skill?: undefined;
    }
  | {
      mode: "version";
      onClose: () => void;
      onSaved: (details: SkillAdminDetails) => void;
      open: boolean;
      /** 当由旧 <Modal> 详情窗切换进入时，原按钮已卸载，关闭弹窗后按此 ref 安全归还焦点至列表入口按钮。 */
      returnFocusRef?: RefObject<HTMLElement | null>;
      skill: SkillAdminDetails | null;
    };

/**
 * 管理端在线 Markdown 模板技能双模式弹窗（基于 Radix Dialog）：
 * - mode="create"：新建公共/部门在线模板技能（支持 .md 解析回填、角色感知范围与附加共享、首页推荐与引导语）；
 * - mode="version"：为已有 format="markdown" 技能在线编辑并发布不可变新版本（支持版本标签、立即激活与乐观锁）。
 */
export function AdminSkillMarkdownDialog(props: AdminSkillMarkdownDialogProps) {
  const { mode, onClose } = props;
  const formId = useId();
  const guideId = useId();
  const form = useAdminSkillMarkdownForm(props);

  return (
    <Dialog onOpenChange={form.handleOpenChange} open={form.isDialogOpen}>
      <DialogContent
        className="max-w-5xl"
        closeDisabled={form.busy}
        data-testid="admin-skill-markdown-dialog"
        onCloseAutoFocus={form.handleCloseAutoFocus}
        onOpenAutoFocus={form.handleOpenAutoFocus}
        size="lg"
      >
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2">
            <FileCode2 aria-hidden="true" className="h-4 w-4 text-brand" />
            <span>
              {mode === "create"
                ? "新建在线模板技能"
                : `在线编辑并发布新版本：${form.targetSkill?.name ?? ""}`}
            </span>
            <Badge variant="info">在线模板</Badge>
          </DialogTitle>
          <DialogDescription>
            {mode === "create"
              ? "纯 Markdown 技能无需打包 ZIP，支持双栏实时预览与导入 .md 自动回填；创建后锁定为在线模板形态。"
              : `基于当前 revision ${form.baselineRevision ?? "—"}（slug: ${form.targetSkill?.slug ?? ""}）追加不可变 Markdown 新版本；仅支持在线模板技能。`}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <form
            className="space-y-4"
            id={formId}
            noValidate
            onSubmit={(event) => {
              event.preventDefault();
              void form.handleSubmit();
            }}
          >
            <AdminSkillMarkdownMetaFields
              activeOrgDepartments={form.activeOrgDepartments}
              busy={form.busy}
              defaultOwnerIncludeSub={form.defaultOwnerIncludeSub}
              deptAdmin={form.deptAdmin}
              description={form.description}
              descriptionLength={form.descriptionLength}
              fieldErrors={form.fieldErrors}
              hasDepartmentControl={form.hasDepartmentControl}
              isDialogOpen={form.isDialogOpen}
              managedDeptOptions={form.managedDeptOptions}
              mode={mode}
              name={form.name}
              nameInputRef={form.nameInputRef}
              nameLength={form.nameLength}
              noManagedDepartments={form.noManagedDepartments}
              onDescriptionChange={form.handleDescriptionChange}
              onNameChange={form.handleNameChange}
              onOwnerDepartmentChange={form.handleOwnerDepartmentChange}
              onScopeChange={form.handleScopeChange}
              onSlugChange={form.handleSlugChange}
              onVersionLabelChange={form.handleVersionLabelChange}
              onVisibilityGrantsChange={form.handleVisibilityGrantsChange}
              orgDepartmentsError={form.orgDepartmentsError}
              orgDepartmentsPending={form.orgDepartmentsPending}
              ownerDepartmentId={form.ownerDepartmentId}
              ownerDepartmentName={form.ownerDepartmentName}
              scope={form.scope}
              slug={form.slug}
              superAdmin={form.superAdmin}
              versionLabel={form.versionLabel}
              visibilityGrants={form.visibilityGrants}
            />

            {/* 双栏 Markdown 指南编辑器（支持一键导入 .md 服务端解析回填） */}
            <div className="space-y-1.5">
              <label
                className="block text-xs font-semibold text-text-primary"
                htmlFor={guideId}
              >
                技能指南正文（Markdown）
              </label>
              <MarkdownSplitEditor
                disabled={form.busy}
                error={form.fieldErrors.guide}
                id={guideId}
                importTriggerLabel="导入 .md"
                importingMarkdown={form.parsing}
                label="技能指南正文（Markdown）"
                maxLength={PERSONAL_SKILL_GUIDE_MAX_LENGTH}
                minHeightClassName="min-h-[280px]"
                onChange={form.handleGuideChange}
                onImportMarkdownFile={(file) =>
                  void form.handleImportMarkdownFile(file)
                }
                placeholder={
                  "# 角色定位\n\n请描述本技能的职责与工作步骤。\n\n## 输出要求\n- 逐项核对关键信息\n- 使用清晰表格汇总结论"
                }
                rows={12}
                value={form.guide}
              />

              {form.importNotice ? (
                <p
                  className="flex items-center gap-1.5 text-xs font-medium text-brand"
                  role="status"
                >
                  <Sparkles aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
                  <span>{form.importNotice}</span>
                </p>
              ) : null}
              {form.importError ? (
                <InlineAlert description={form.importError} variant="danger" />
              ) : null}
            </div>

            <AdminSkillMarkdownPublishFields
              activate={form.activate}
              busy={form.busy}
              enabled={form.enabled}
              featured={form.featured}
              fieldErrors={form.fieldErrors}
              mode={mode}
              onActivateChange={form.handleActivateChange}
              onEnabledChange={form.handleEnabledChange}
              onFeaturedChange={form.handleFeaturedChange}
              onStarterPromptChange={form.handleStarterPromptChange}
              starterPrompt={form.starterPrompt}
              starterPromptLength={form.starterPromptLength}
              starterPromptOverLimit={form.starterPromptOverLimit}
            />

            {form.revisionConflict ? (
              <InlineAlert
                description="该技能已在其他窗口被更新（revision 已变更）。请先关闭本窗口并在技能详情中刷新最新内容后再发布新版本，避免覆盖他人变更。"
                title="修订号冲突"
                variant="warning"
              />
            ) : null}

            {form.requestError ? (
              <InlineAlert description={form.requestError} variant="danger" />
            ) : null}
          </form>
        </DialogBody>

        <DialogFooter>
          <Button
            disabled={form.busy}
            onClick={onClose}
            type="button"
            variant="outline"
          >
            取消
          </Button>
          <Button
            disabled={form.busy || form.noManagedDepartments}
            form={formId}
            loading={form.submitting}
            type="submit"
            variant="primary"
          >
            {mode === "create"
              ? form.enabled
                ? "创建并启用模板技能"
                : "创建模板技能"
              : form.activate
                ? "发布并设为当前版本"
                : "仅发布新版本"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
