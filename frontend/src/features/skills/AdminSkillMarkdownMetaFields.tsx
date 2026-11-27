import { useId, type RefObject } from "react";

import {
  InlineAlert,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  VisibilityBadge,
  cn,
} from "../../shared/ui";
import { VisibilityGrantsEditor } from "../access/VisibilityGrantsEditor";
import type { OrgDepartment } from "../admin/model";
import type { VisibilityGrantInput } from "../knowledge/model";
import {
  PERSONAL_SKILL_DESCRIPTION_MAX_LENGTH,
  PERSONAL_SKILL_NAME_MAX_LENGTH,
  SKILL_SLUG_MAX_LENGTH,
  SKILL_STARTER_PROMPT_MAX_LENGTH,
  SKILL_VERSION_LABEL_MAX_LENGTH,
} from "./model";
import type {
  ManagedDepartmentOption,
  MarkdownFormErrors,
} from "./useAdminSkillMarkdownForm";

function joinIds(...ids: Array<string | false | undefined>): string | undefined {
  const joined = ids.filter(Boolean).join(" ");
  return joined || undefined;
}

export interface AdminSkillMarkdownMetaFieldsProps {
  activeOrgDepartments: OrgDepartment[];
  busy: boolean;
  defaultOwnerIncludeSub: boolean;
  deptAdmin: boolean;
  description: string;
  descriptionLength: number;
  fieldErrors: MarkdownFormErrors;
  hasDepartmentControl: boolean;
  isDialogOpen: boolean;
  managedDeptOptions: ManagedDepartmentOption[];
  mode: "create" | "version";
  name: string;
  nameInputRef: RefObject<HTMLInputElement>;
  nameLength: number;
  noManagedDepartments: boolean;
  onDescriptionChange: (value: string) => void;
  onNameChange: (value: string) => void;
  onOwnerDepartmentChange: (deptId: string, trimForGrant?: boolean) => void;
  onScopeChange: (scope: "company" | "department") => void;
  onSlugChange: (value: string) => void;
  onVersionLabelChange: (value: string) => void;
  onVisibilityGrantsChange: (grants: VisibilityGrantInput[]) => void;
  orgDepartmentsError: boolean;
  orgDepartmentsPending: boolean;
  ownerDepartmentId: string;
  ownerDepartmentName: string | null;
  scope: "company" | "department";
  slug: string;
  superAdmin: boolean;
  versionLabel: string;
  visibilityGrants: VisibilityGrantInput[];
}

/**
 * 技能基础元数据与部门可见范围授权区块：
 * - 上半区：技能名称、自定义 Slug（新建）或版本标签（发版）、技能简介；
 * - 范围区（仅新建）：发布范围、归属部门选择（兼容部门管理员、超管目录及失败降级手输）与 VisibilityGrantsEditor。
 */
export function AdminSkillMarkdownMetaFields({
  activeOrgDepartments,
  busy,
  defaultOwnerIncludeSub,
  deptAdmin,
  description,
  descriptionLength,
  fieldErrors,
  hasDepartmentControl,
  isDialogOpen,
  managedDeptOptions,
  mode,
  name,
  nameInputRef,
  nameLength,
  noManagedDepartments,
  onDescriptionChange,
  onNameChange,
  onOwnerDepartmentChange,
  onScopeChange,
  onSlugChange,
  onVersionLabelChange,
  onVisibilityGrantsChange,
  orgDepartmentsError,
  orgDepartmentsPending,
  ownerDepartmentId,
  ownerDepartmentName,
  scope,
  slug,
  superAdmin,
  versionLabel,
  visibilityGrants,
}: AdminSkillMarkdownMetaFieldsProps) {
  const nameId = useId();
  const nameErrorId = useId();
  const slugId = useId();
  const slugErrorId = useId();
  const descriptionId = useId();
  const descriptionErrorId = useId();
  const scopeId = useId();
  const departmentIdInput = useId();
  const departmentErrorId = useId();
  const versionLabelId = useId();
  const versionLabelErrorId = useId();

  const departmentSelectOptions = deptAdmin
    ? managedDeptOptions
    : activeOrgDepartments;

  return (
    <>
      {/* 基础元数据：名称 + 自定义标识（新建）或版本标签（发版） */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <label
              className="text-xs font-semibold text-text-primary"
              htmlFor={nameId}
            >
              技能名称
            </label>
            <span
              className={cn(
                "text-xs tabular-nums",
                nameLength > PERSONAL_SKILL_NAME_MAX_LENGTH
                  ? "font-semibold text-danger"
                  : "text-text-tertiary",
              )}
            >
              {nameLength} / {PERSONAL_SKILL_NAME_MAX_LENGTH}
            </span>
          </div>
          <Input
            aria-describedby={fieldErrors.name ? nameErrorId : undefined}
            autoComplete="off"
            disabled={busy}
            error={Boolean(fieldErrors.name)}
            id={nameId}
            onChange={(event) => onNameChange(event.target.value)}
            placeholder="例如：财务报销合规审核助手"
            ref={nameInputRef}
            value={name}
          />
          {fieldErrors.name ? (
            <p
              className="text-xs font-medium text-danger"
              id={nameErrorId}
              role="alert"
            >
              {fieldErrors.name}
            </p>
          ) : null}
        </div>

        {mode === "create" ? (
          <div className="space-y-1.5">
            <div className="flex items-center justify-between gap-2">
              <label
                className="text-xs font-semibold text-text-primary"
                htmlFor={slugId}
              >
                自定义标识（可选）
              </label>
              <span className="text-xs text-text-tertiary">留空自动生成</span>
            </div>
            <Input
              aria-describedby={fieldErrors.slug ? slugErrorId : undefined}
              autoComplete="off"
              disabled={busy}
              error={Boolean(fieldErrors.slug)}
              id={slugId}
              maxLength={SKILL_SLUG_MAX_LENGTH}
              onChange={(event) => onSlugChange(event.target.value)}
              placeholder="例如：fin-expense-audit（小写字母/数字/连字符）"
              value={slug}
            />
            {fieldErrors.slug ? (
              <p
                className="text-xs font-medium text-danger"
                id={slugErrorId}
                role="alert"
              >
                {fieldErrors.slug}
              </p>
            ) : null}
          </div>
        ) : (
          <div className="space-y-1.5">
            <div className="flex items-center justify-between gap-2">
              <label
                className="text-xs font-semibold text-text-primary"
                htmlFor={versionLabelId}
              >
                版本标签（可选）
              </label>
              <span className="text-xs tabular-nums text-text-tertiary">
                {Array.from(versionLabel.trim()).length} /{" "}
                {SKILL_VERSION_LABEL_MAX_LENGTH}
              </span>
            </div>
            <Input
              aria-describedby={
                fieldErrors.label ? versionLabelErrorId : undefined
              }
              autoComplete="off"
              disabled={busy}
              error={Boolean(fieldErrors.label)}
              id={versionLabelId}
              onChange={(event) => onVersionLabelChange(event.target.value)}
              placeholder="例如：v2.0-2026Q4（留空将自动按序号命名）"
              value={versionLabel}
            />
            {fieldErrors.label ? (
              <p
                className="text-xs font-medium text-danger"
                id={versionLabelErrorId}
                role="alert"
              >
                {fieldErrors.label}
              </p>
            ) : null}
          </div>
        )}
      </div>

      {/* 技能简介 */}
      <div className="space-y-1.5">
        <div className="flex items-center justify-between gap-2">
          <label
            className="text-xs font-semibold text-text-primary"
            htmlFor={descriptionId}
          >
            技能简介
          </label>
          <span
            className={cn(
              "text-xs tabular-nums",
              descriptionLength > PERSONAL_SKILL_DESCRIPTION_MAX_LENGTH
                ? "font-semibold text-danger"
                : "text-text-tertiary",
            )}
          >
            {descriptionLength} / {PERSONAL_SKILL_DESCRIPTION_MAX_LENGTH}
          </span>
        </div>
        <textarea
          aria-describedby={
            fieldErrors.description ? descriptionErrorId : undefined
          }
          aria-invalid={fieldErrors.description ? true : undefined}
          className={cn(
            "block w-full rounded-lg border bg-canvas px-3 py-2 text-sm text-text-primary shadow-xs transition-colors",
            "placeholder:text-text-placeholder focus-visible:outline-hidden focus-visible:ring-2",
            "disabled:cursor-not-allowed disabled:bg-subtle disabled:text-text-tertiary",
            fieldErrors.description
              ? "border-danger focus-visible:border-danger focus-visible:ring-danger/20"
              : "border-border-default hover:border-border-strong focus-visible:border-ring-focus focus-visible:ring-ring-focus/20",
          )}
          disabled={busy}
          id={descriptionId}
          onChange={(event) => onDescriptionChange(event.target.value)}
          placeholder="简要说明该技能的适用办公场景与核心交付"
          rows={2}
          value={description}
        />
        {fieldErrors.description ? (
          <p
            className="text-xs font-medium text-danger"
            id={descriptionErrorId}
            role="alert"
          >
            {fieldErrors.description}
          </p>
        ) : null}
      </div>

      {/* 可见范围与归属部门（仅新建模式可配置） */}
      {mode === "create" ? (
        <div className="space-y-3 rounded-xl border border-border-default bg-subtle/40 p-3.5">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div className="space-y-1.5">
              {deptAdmin ? (
                <>
                  <span className="block text-xs font-semibold text-text-primary">
                    发布范围
                  </span>
                  <div className="flex flex-wrap items-center gap-2 pt-1">
                    <VisibilityBadge scope="department" />
                    <span className="text-xs text-text-tertiary">
                      部门管理员固定发布为所管辖部门共享技能
                    </span>
                  </div>
                </>
              ) : (
                <>
                  <label
                    className="block text-xs font-semibold text-text-primary"
                    htmlFor={scopeId}
                  >
                    发布范围
                  </label>
                  <Select
                    disabled={busy}
                    onValueChange={(value) =>
                      onScopeChange(value as "company" | "department")
                    }
                    value={scope}
                  >
                    <SelectTrigger aria-label="发布范围" id={scopeId}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="company">全公司</SelectItem>
                      <SelectItem value="department">部门共享</SelectItem>
                    </SelectContent>
                  </Select>
                </>
              )}
            </div>

            {scope === "department" ? (
              <div className="space-y-1.5">
                {hasDepartmentControl ? (
                  <label
                    className="block text-xs font-semibold text-text-primary"
                    htmlFor={departmentIdInput}
                  >
                    归属部门
                  </label>
                ) : (
                  <span className="block text-xs font-semibold text-text-primary">
                    归属部门
                  </span>
                )}

                {noManagedDepartments ? (
                  <InlineAlert
                    description="当前账号尚未配置可管辖部门，无法新建部门技能，请联系企业超级管理员配置管辖范围。"
                    title="暂无可管辖部门"
                    variant="warning"
                  />
                ) : !deptAdmin && orgDepartmentsPending ? (
                  <div
                    className="flex h-9 items-center rounded-lg border border-border-subtle bg-canvas px-3 text-xs text-text-tertiary"
                    role="status"
                  >
                    正在加载企业活跃部门列表…
                  </div>
                ) : !deptAdmin && orgDepartmentsError ? (
                  <div className="space-y-1">
                    <Input
                      aria-describedby={
                        fieldErrors.ownerDepartmentId
                          ? departmentErrorId
                          : undefined
                      }
                      aria-label="归属部门"
                      autoComplete="off"
                      disabled={busy}
                      error={Boolean(fieldErrors.ownerDepartmentId)}
                      id={departmentIdInput}
                      onChange={(event) =>
                        onOwnerDepartmentChange(event.target.value, true)
                      }
                      placeholder="请输入归属部门 ID（如 dept_fin）"
                      value={ownerDepartmentId}
                    />
                    <p className="text-[11px] text-text-tertiary">
                      部门目录加载失败，已降级为手动输入部门 ID。
                    </p>
                  </div>
                ) : (
                  <Select
                    disabled={busy}
                    onValueChange={(value) => onOwnerDepartmentChange(value)}
                    value={ownerDepartmentId}
                  >
                    <SelectTrigger
                      aria-describedby={
                        fieldErrors.ownerDepartmentId
                          ? departmentErrorId
                          : undefined
                      }
                      aria-label="归属部门"
                      error={Boolean(fieldErrors.ownerDepartmentId)}
                      id={departmentIdInput}
                    >
                      <SelectValue placeholder="请选择归属部门" />
                    </SelectTrigger>
                    <SelectContent>
                      {departmentSelectOptions.map((dept) => (
                        <SelectItem key={dept.id} value={dept.id}>
                          {dept.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}

                {fieldErrors.ownerDepartmentId ? (
                  <p
                    className="text-xs font-medium text-danger"
                    id={departmentErrorId}
                    role="alert"
                  >
                    {fieldErrors.ownerDepartmentId}
                  </p>
                ) : null}
              </div>
            ) : null}
          </div>

          {scope === "department" && ownerDepartmentId.trim() ? (
            <VisibilityGrantsEditor
              defaultIncludeSubdepartments={defaultOwnerIncludeSub}
              departments={superAdmin ? activeOrgDepartments : undefined}
              departmentsError={superAdmin ? orgDepartmentsError : false}
              departmentsLoading={superAdmin ? orgDepartmentsPending : false}
              disabled={busy}
              enabled={isDialogOpen}
              onChange={onVisibilityGrantsChange}
              ownerDepartmentId={ownerDepartmentId}
              ownerDepartmentName={ownerDepartmentName}
              resourceLabel="技能"
              value={visibilityGrants}
            />
          ) : null}
        </div>
      ) : null}
    </>
  );
}

export interface AdminSkillMarkdownPublishFieldsProps {
  activate: boolean;
  busy: boolean;
  enabled: boolean;
  featured: boolean;
  fieldErrors: MarkdownFormErrors;
  mode: "create" | "version";
  onActivateChange: (checked: boolean) => void;
  onEnabledChange: (checked: boolean) => void;
  onFeaturedChange: (checked: boolean) => void;
  onStarterPromptChange: (value: string) => void;
  starterPrompt: string;
  starterPromptLength: number;
  starterPromptOverLimit: boolean;
}

/** 发布与首页快捷应用设置（新建模式）或立即激活版本开关（发版模式）。 */
export function AdminSkillMarkdownPublishFields({
  activate,
  busy,
  enabled,
  featured,
  fieldErrors,
  mode,
  onActivateChange,
  onEnabledChange,
  onFeaturedChange,
  onStarterPromptChange,
  starterPrompt,
  starterPromptLength,
  starterPromptOverLimit,
}: AdminSkillMarkdownPublishFieldsProps) {
  const enabledId = useId();
  const featuredId = useId();
  const starterPromptId = useId();
  const starterPromptErrorId = useId();
  const activateId = useId();

  if (mode === "create") {
    return (
      <div className="space-y-3.5 rounded-xl border border-border-default bg-subtle/40 p-3.5">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <label
            className="inline-flex cursor-pointer items-center gap-2 text-xs font-medium text-text-primary select-none"
            htmlFor={enabledId}
          >
            <input
              checked={enabled}
              className="h-4 w-4 rounded border-border-strong accent-brand"
              disabled={busy}
              id={enabledId}
              onChange={(event) => onEnabledChange(event.target.checked)}
              type="checkbox"
            />
            <span>创建后立即启用（对可见范围成员生效）</span>
          </label>

          <div className="flex items-center gap-2.5">
            <Switch
              aria-label="推荐至工作台首页快捷应用"
              checked={featured}
              disabled={busy}
              id={featuredId}
              onCheckedChange={onFeaturedChange}
              size="sm"
            />
            <label
              className="cursor-pointer text-xs font-medium text-text-primary select-none"
              htmlFor={featuredId}
            >
              推荐至工作台首页快捷应用
            </label>
          </div>
        </div>

        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <label
              className="text-xs font-semibold text-text-primary"
              htmlFor={starterPromptId}
            >
              首页默认引导语（可选）
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
            aria-describedby={joinIds(
              fieldErrors.starterPrompt ? starterPromptErrorId : undefined,
            )}
            aria-invalid={
              fieldErrors.starterPrompt || starterPromptOverLimit
                ? true
                : undefined
            }
            className={cn(
              "block w-full rounded-lg border bg-canvas px-3 py-2 text-sm text-text-primary shadow-xs transition-colors",
              "placeholder:text-text-placeholder focus-visible:outline-hidden focus-visible:ring-2",
              "disabled:cursor-not-allowed disabled:bg-subtle disabled:text-text-tertiary",
              fieldErrors.starterPrompt || starterPromptOverLimit
                ? "border-danger focus-visible:border-danger focus-visible:ring-danger/20"
                : "border-border-default hover:border-border-strong focus-visible:border-ring-focus focus-visible:ring-ring-focus/20",
            )}
            disabled={busy}
            id={starterPromptId}
            onChange={(event) => onStarterPromptChange(event.target.value)}
            placeholder="点击工作台首页快捷应用卡片时，若输入框为空将自动填入此引导语"
            rows={2}
            value={starterPrompt}
          />
          {fieldErrors.starterPrompt ? (
            <p
              className="text-xs font-medium text-danger"
              id={starterPromptErrorId}
              role="alert"
            >
              {fieldErrors.starterPrompt}
            </p>
          ) : null}
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-border-default bg-subtle/40 p-3.5">
      <label
        className="inline-flex cursor-pointer items-center gap-2 text-xs font-medium text-text-primary select-none"
        htmlFor={activateId}
      >
        <input
          checked={activate}
          className="h-4 w-4 rounded border-border-strong accent-brand"
          disabled={busy}
          id={activateId}
          onChange={(event) => onActivateChange(event.target.checked)}
          type="checkbox"
        />
        <span>发布后立即设为当前生效版本</span>
      </label>
      <p className="mt-1 pl-6 text-xs text-text-tertiary">
        取消勾选时仅追加不可变版本历史，当前生效版本保持不变，稍后可在技能详情中手动切换。
      </p>
    </div>
  );
}
