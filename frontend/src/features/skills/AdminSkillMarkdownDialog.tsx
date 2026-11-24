import { FileCode2, Sparkles } from "lucide-react";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";

import { ApiError } from "../../shared/api/client";
import { newRequestId } from "../../shared/lib/requestId";
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
  Input,
  MarkdownSplitEditor,
  Switch,
  VisibilityBadge,
  cn,
  toast,
} from "../../shared/ui";
import {
  VisibilityGrantsEditor,
  defaultOwnerGrant,
  isDefaultVisibilityGrants,
  normalizeVisibilityGrants,
} from "../access/VisibilityGrantsEditor";
import { useAdminOrgDepartments } from "../admin/hooks";
import { useAuth } from "../auth/AuthProvider";
import {
  isDeptAdmin,
  isSuperAdmin,
  resolveUserDepartmentName,
} from "../auth/model";
import type { VisibilityGrantInput } from "../knowledge/model";
import {
  isSkillRevisionConflict,
  skillErrorMessage,
} from "./errorMessages";
import {
  useCreateAdminMarkdownSkillMutation,
  useParseAdminSkillMarkdownMutation,
  usePublishAdminMarkdownVersionMutation,
} from "./hooks";
import {
  PERSONAL_SKILL_DESCRIPTION_MAX_LENGTH,
  PERSONAL_SKILL_GUIDE_MAX_LENGTH,
  PERSONAL_SKILL_NAME_MAX_LENGTH,
  SKILL_SLUG_MAX_LENGTH,
  SKILL_STARTER_PROMPT_MAX_LENGTH,
  SKILL_VERSION_LABEL_MAX_LENGTH,
  normalizeStarterPrompt,
  validateOptionalAdminSlug,
  validatePersonalSkillFile,
  validatePersonalSkillInput,
  validateStarterPrompt,
  validateVersionLabel,
  type AdminSkillMarkdownCreateInput,
  type AdminSkillMarkdownVersionInput,
  type SkillAdminDetails,
} from "./model";

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

interface MarkdownFormErrors {
  description?: string;
  guide?: string;
  label?: string;
  name?: string;
  ownerDepartmentId?: string;
  slug?: string;
  starterPrompt?: string;
}

function joinIds(...ids: Array<string | false | undefined>): string | undefined {
  const joined = ids.filter(Boolean).join(" ");
  return joined || undefined;
}

/**
 * 管理端在线 Markdown 模板技能双模式弹窗（基于 Radix Dialog）：
 * - mode="create"：新建公共/部门在线模板技能（支持 .md 解析回填、角色感知范围与附加共享、首页推荐与引导语）；
 * - mode="version"：为已有 format="markdown" 技能在线编辑并发布不可变新版本（支持版本标签、立即激活与乐观锁）。
 */
export function AdminSkillMarkdownDialog(props: AdminSkillMarkdownDialogProps) {
  const { mode, onClose, onSaved, open, returnFocusRef } = props;
  const targetSkill = mode === "version" ? props.skill : null;
  const { user } = useAuth();
  const superAdmin = isSuperAdmin(user);
  const deptAdmin = isDeptAdmin(user);
  // 部门管理员归属部门默认行使用 include_subdepartments=false，避免直属管辖部门管理员误触 403；
  // 超级管理员默认 true。
  const defaultOwnerIncludeSub = superAdmin;

  const formId = useId();
  const nameId = useId();
  const nameErrorId = useId();
  const slugId = useId();
  const slugErrorId = useId();
  const descriptionId = useId();
  const descriptionErrorId = useId();
  const guideId = useId();
  const scopeId = useId();
  const departmentIdInput = useId();
  const departmentErrorId = useId();
  const enabledId = useId();
  const featuredId = useId();
  const starterPromptId = useId();
  const starterPromptErrorId = useId();
  const versionLabelId = useId();
  const versionLabelErrorId = useId();
  const activateId = useId();

  const nameInputRef = useRef<HTMLInputElement>(null);
  const requestIdRef = useRef<string | null>(null);
  const submitLockRef = useRef(false);
  // 受控 Radix Dialog 无 DialogTrigger：打开瞬间同步捕获原焦点元素，关闭时在 onCloseAutoFocus 归还焦点。
  const returnFocusElementRef = useRef<HTMLElement | null>(null);
  const focusCaptureWasOpenRef = useRef(false);
  const initWasOpenRef = useRef(false);

  const managedDeptOptions = useMemo(() => {
    if (!user) {
      return [];
    }
    return user.managed_department_ids.map((id) => ({
      id,
      name: resolveUserDepartmentName(user, id) ?? id,
    }));
  }, [user]);

  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [description, setDescription] = useState("");
  const [guide, setGuide] = useState("");
  const [scope, setScope] = useState<"company" | "department">(
    deptAdmin ? "department" : "company",
  );
  const [ownerDepartmentId, setOwnerDepartmentId] = useState("");
  const [visibilityGrants, setVisibilityGrants] = useState<
    VisibilityGrantInput[]
  >([]);
  // 追踪管理员是否主动修改过可见性附加授权；未改动时省略 visibility_grants 交由后端按实时管辖子树派生。
  const [visibilityGrantsTouched, setVisibilityGrantsTouched] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [featured, setFeatured] = useState(false);
  const [starterPrompt, setStarterPrompt] = useState("");
  const [versionLabel, setVersionLabel] = useState("");
  const [activate, setActivate] = useState(true);
  const [baselineRevision, setBaselineRevision] = useState<number | null>(null);

  const [fieldErrors, setFieldErrors] = useState<MarkdownFormErrors>({});
  const [importNotice, setImportNotice] = useState<string | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [revisionConflict, setRevisionConflict] = useState(false);

  const parseMutation = useParseAdminSkillMarkdownMutation();
  const createMutation = useCreateAdminMarkdownSkillMutation();
  const publishMutation = usePublishAdminMarkdownVersionMutation(
    targetSkill?.id ?? "",
  );
  const { reset: resetParseMutation } = parseMutation;
  const { reset: resetCreateMutation } = createMutation;
  const { reset: resetPublishMutation } = publishMutation;

  const isDialogOpen = open && (mode === "create" || Boolean(targetSkill));
  const orgDepartments = useAdminOrgDepartments(
    isDialogOpen && mode === "create" && superAdmin && scope === "department",
    false,
  );
  const activeOrgDepartments = useMemo(
    () =>
      (orgDepartments.data?.items ?? []).filter(
        (dept) => dept.status === "active",
      ),
    [orgDepartments.data?.items],
  );

  useLayoutEffect(() => {
    if (isDialogOpen && !focusCaptureWasOpenRef.current) {
      const active =
        typeof document !== "undefined" ? document.activeElement : null;
      returnFocusElementRef.current =
        active instanceof HTMLElement && active !== document.body
          ? active
          : null;
    }
    focusCaptureWasOpenRef.current = isDialogOpen;
  }, [isDialogOpen]);

  // 仅在弹窗从关闭切为打开时初始化草稿，防止后台 /auth/me 刷新引起依赖变化误清空管理员正在编辑的正文。
  useEffect(() => {
    const justOpened = isDialogOpen && !initWasOpenRef.current;
    initWasOpenRef.current = isDialogOpen;
    if (!justOpened) {
      return;
    }
    requestIdRef.current = null;
    submitLockRef.current = false;
    setFieldErrors({});
    setImportNotice(null);
    setImportError(null);
    setRequestError(null);
    setRevisionConflict(false);
    resetParseMutation();
    resetCreateMutation();
    resetPublishMutation();

    if (mode === "create") {
      const initialScope: "company" | "department" = deptAdmin
        ? "department"
        : "company";
      const initialDeptId =
        deptAdmin && managedDeptOptions.length === 1
          ? managedDeptOptions[0].id
          : "";
      setName("");
      setSlug("");
      setDescription("");
      setGuide("");
      setScope(initialScope);
      setOwnerDepartmentId(initialDeptId);
      setVisibilityGrants(
        initialDeptId
          ? [defaultOwnerGrant(initialDeptId, defaultOwnerIncludeSub)]
          : [],
      );
      setVisibilityGrantsTouched(false);
      setEnabled(true);
      setFeatured(false);
      setStarterPrompt("");
      setVersionLabel("");
      setActivate(true);
      setBaselineRevision(null);
    } else if (targetSkill) {
      const currentVersion =
        targetSkill.versions.find(
          (version) => version.id === targetSkill.current_version_id,
        ) ?? targetSkill.versions[0];
      setName(currentVersion?.name ?? targetSkill.name);
      setSlug(targetSkill.slug);
      setDescription(currentVersion?.description ?? targetSkill.description);
      setGuide(currentVersion?.guide ?? "");
      setVersionLabel("");
      setActivate(true);
      setBaselineRevision(targetSkill.revision);
    }
  }, [
    defaultOwnerIncludeSub,
    deptAdmin,
    isDialogOpen,
    managedDeptOptions,
    mode,
    resetCreateMutation,
    resetParseMutation,
    resetPublishMutation,
    targetSkill,
  ]);

  const submitting = createMutation.isPending || publishMutation.isPending;
  const parsing = parseMutation.isPending;
  const busy = submitting || parsing;
  const noManagedDepartments =
    mode === "create" && deptAdmin && managedDeptOptions.length === 0;
  const hasDepartmentControl =
    !noManagedDepartments && !(superAdmin && orgDepartments.isPending);

  const rotateIdempotencyKey = () => {
    requestIdRef.current = null;
  };

  const clearFieldError = (field: keyof MarkdownFormErrors) => {
    setFieldErrors((current) =>
      current[field] ? { ...current, [field]: undefined } : current,
    );
  };

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen && !busy && !submitLockRef.current) {
      onClose();
    }
  };

  const handleImportMarkdownFile = async (file: File) => {
    if (busy) {
      return;
    }
    setImportNotice(null);
    setImportError(null);
    setRequestError(null);
    const validation = validatePersonalSkillFile(file);
    if (validation) {
      setImportError(validation);
      return;
    }
    try {
      const draft = await parseMutation.mutateAsync(file);
      const hasParsedName = Boolean(draft.name.trim());
      const hasParsedDescription = Boolean(draft.description.trim());
      // 无 YAML frontmatter 时解析结果可能为空名称/空简介，仅在非空时覆盖，防止清空管理员已填内容。
      if (hasParsedName) {
        setName(draft.name);
      }
      if (hasParsedDescription) {
        setDescription(draft.description);
      }
      setGuide(draft.guide);
      setFieldErrors((current) => ({
        ...current,
        description: hasParsedDescription ? undefined : current.description,
        guide: undefined,
        name: hasParsedName ? undefined : current.name,
      }));
      rotateIdempotencyKey();
      setImportNotice(
        hasParsedName || hasParsedDescription
          ? "已从 Markdown 文件解析并回填名称、简介与正文，尚未发布，请核对后提交。"
          : "已导入 Markdown 正文（文件未包含名称/简介元数据，已保留原填写内容），尚未发布，请核对后提交。",
      );
    } catch (error) {
      // 解析失败仅提示文件问题，绝不覆盖管理员已输入的草稿。
      setImportError(skillErrorMessage(error));
    }
  };

  const handleSubmit = async () => {
    if (busy || submitLockRef.current || noManagedDepartments) {
      return;
    }

    const baseErrors = validatePersonalSkillInput({
      description,
      guide,
      name,
    });
    const nextErrors: MarkdownFormErrors = { ...baseErrors };

    if (mode === "create") {
      const slugErr = validateOptionalAdminSlug(slug);
      if (slugErr) {
        nextErrors.slug = slugErr;
      }
      const promptErr = validateStarterPrompt(starterPrompt);
      if (promptErr) {
        nextErrors.starterPrompt = promptErr;
      }
      if (scope === "department" && !ownerDepartmentId.trim()) {
        nextErrors.ownerDepartmentId = "请选择或填写部门技能的归属部门。";
      }
    } else {
      const labelErr = validateVersionLabel(versionLabel);
      if (labelErr) {
        nextErrors.label = labelErr;
      }
    }

    setFieldErrors(nextErrors);
    setRequestError(null);
    setRevisionConflict(false);
    if (Object.values(nextErrors).some(Boolean)) {
      return;
    }

    const requestId = requestIdRef.current ?? newRequestId();
    requestIdRef.current = requestId;
    submitLockRef.current = true;

    try {
      if (mode === "create") {
        const trimmedOwnerDept = ownerDepartmentId.trim();
        const normalizedSlug = slug.trim();
        const normalizedPrompt = normalizeStarterPrompt(starterPrompt);
        const normalizedGrants =
          scope === "department" && trimmedOwnerDept
            ? normalizeVisibilityGrants(
                trimmedOwnerDept,
                visibilityGrants,
                defaultOwnerIncludeSub,
              )
            : [];
        // 用户未改动可见性配置时省略 visibility_grants，交由后端按实时管辖子树派生准确默认授权；
        // 追加部门/角色或手动切换开关后，提交规范化去重后的完整数组。
        const shouldSendVisibilityGrants =
          scope === "department" &&
          (visibilityGrantsTouched ||
            !isDefaultVisibilityGrants(
              trimmedOwnerDept,
              normalizedGrants,
              defaultOwnerIncludeSub,
            ));

        // 严格遵循契约 3.5 extra="forbid"：仅在需要时携带可选字段，不发送冗余键。
        const payload: AdminSkillMarkdownCreateInput = {
          description: description.trim(),
          enabled,
          featured,
          guide,
          name: name.trim(),
          scope,
          ...(scope === "department"
            ? {
                owner_department_id: trimmedOwnerDept,
                ...(shouldSendVisibilityGrants
                  ? { visibility_grants: normalizedGrants }
                  : {}),
              }
            : {}),
          ...(normalizedSlug ? { slug: normalizedSlug } : {}),
          ...(normalizedPrompt !== null
            ? { starter_prompt: normalizedPrompt }
            : {}),
        };

        const created = await createMutation.mutateAsync({
          input: payload,
          requestId,
        });
        requestIdRef.current = null;
        toast.success(`在线模板技能「${created.name}」已创建`);
        onSaved(created);
      } else if (targetSkill && baselineRevision !== null) {
        const trimmedLabel = versionLabel.trim();
        // 严格遵循契约 3.6 extra="forbid"：携带 expected_revision 乐观锁与 activate 开关。
        const payload: AdminSkillMarkdownVersionInput = {
          activate,
          description: description.trim(),
          expected_revision: baselineRevision,
          guide,
          name: name.trim(),
          ...(trimmedLabel ? { label: trimmedLabel } : {}),
        };

        const updated = await publishMutation.mutateAsync({
          input: payload,
          requestId,
        });
        requestIdRef.current = null;
        toast.success(
          activate
            ? `已发布并激活新版本（revision ${updated.revision}）`
            : `已保存新版本草稿（revision ${updated.revision}），当前生效版本未变更`,
        );
        onSaved(updated);
      }
    } catch (error) {
      const message = skillErrorMessage(error);
      if (mode === "version" && isSkillRevisionConflict(error)) {
        // 409 修订号冲突统一通过顶部警告横幅展示，避免同时渲染重复的 danger requestError。
        setRevisionConflict(true);
        setRequestError(null);
      } else {
        setRequestError(message);
      }
      if (error instanceof ApiError) {
        // 同键异参冲突时立即轮换幂等键，网络或 5xx 失败则保留原键以支持安全重试。
        if (error.detail === "idempotency_conflict") {
          requestIdRef.current = null;
        }
        // 同时兼容结构化 detail.field 与纯字符串业务错误码，精准标红对应表单输入项。
        if (error.field === "name" || error.detail === "skill_name_invalid") {
          setFieldErrors((current) => ({ ...current, name: message }));
        } else if (
          error.field === "description" ||
          error.detail === "skill_description_invalid"
        ) {
          setFieldErrors((current) => ({ ...current, description: message }));
        } else if (
          error.field === "guide" ||
          error.detail === "skill_guide_invalid" ||
          error.detail === "skill_guide_control_character"
        ) {
          setFieldErrors((current) => ({ ...current, guide: message }));
        } else if (
          error.field === "slug" ||
          error.detail === "skill_slug_conflict" ||
          error.detail === "skill_slug_invalid"
        ) {
          setFieldErrors((current) => ({ ...current, slug: message }));
        } else if (
          error.field === "starter_prompt" ||
          error.detail === "skill_starter_prompt_invalid"
        ) {
          setFieldErrors((current) => ({ ...current, starterPrompt: message }));
        } else if (
          error.field === "label" ||
          error.detail === "skill_version_label_invalid"
        ) {
          setFieldErrors((current) => ({ ...current, label: message }));
        } else if (
          error.detail === "owner_department_not_found" ||
          error.detail === "owner_department_required" ||
          error.detail === "owner_department_not_allowed"
        ) {
          setFieldErrors((current) => ({
            ...current,
            ownerDepartmentId: message,
          }));
        }
      }
    } finally {
      submitLockRef.current = false;
    }
  };

  const nameLength = Array.from(name.trim()).length;
  const descriptionLength = Array.from(description.trim()).length;
  const starterPromptLength = Array.from(starterPrompt.trim()).length;
  const starterPromptOverLimit =
    starterPromptLength > SKILL_STARTER_PROMPT_MAX_LENGTH;

  const ownerDepartmentName = useMemo(() => {
    const trimmed = ownerDepartmentId.trim();
    if (!trimmed) {
      return null;
    }
    const fromOrg = activeOrgDepartments.find((dept) => dept.id === trimmed);
    if (fromOrg) {
      return fromOrg.name;
    }
    return resolveUserDepartmentName(user, trimmed) ?? trimmed;
  }, [activeOrgDepartments, ownerDepartmentId, user]);

  return (
    <Dialog onOpenChange={handleOpenChange} open={isDialogOpen}>
      <DialogContent
        className="max-w-5xl"
        closeDisabled={busy}
        data-testid="admin-skill-markdown-dialog"
        onCloseAutoFocus={(event) => {
          // 归还焦点至触发弹窗的原控件；若原触发钮已随旧 <Modal> 卸载（mode="version"），安全回落到父页面入口按钮。
          const captured = returnFocusElementRef.current;
          returnFocusElementRef.current = null;
          const fallback = returnFocusRef?.current ?? null;
          const target =
            captured && captured.isConnected
              ? captured
              : fallback && fallback.isConnected
                ? fallback
                : null;
          if (target) {
            event.preventDefault();
            target.focus();
          }
        }}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          nameInputRef.current?.focus();
        }}
        size="lg"
      >
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2">
            <FileCode2 aria-hidden="true" className="h-4 w-4 text-brand" />
            <span>
              {mode === "create"
                ? "新建在线模板技能"
                : `在线编辑并发布新版本：${targetSkill?.name ?? ""}`}
            </span>
            <Badge variant="info">在线模板</Badge>
          </DialogTitle>
          <DialogDescription>
            {mode === "create"
              ? "纯 Markdown 技能无需打包 ZIP，支持双栏实时预览与导入 .md 自动回填；创建后锁定为在线模板形态。"
              : `基于当前 revision ${baselineRevision ?? "—"}（slug: ${targetSkill?.slug ?? ""}）追加不可变 Markdown 新版本；仅支持在线模板技能。`}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <form
            className="space-y-4"
            id={formId}
            noValidate
            onSubmit={(event) => {
              event.preventDefault();
              void handleSubmit();
            }}
          >
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
                  aria-describedby={
                    fieldErrors.name ? nameErrorId : undefined
                  }
                  autoComplete="off"
                  disabled={busy}
                  error={Boolean(fieldErrors.name)}
                  id={nameId}
                  onChange={(event) => {
                    setName(event.target.value);
                    clearFieldError("name");
                    setRequestError(null);
                    rotateIdempotencyKey();
                  }}
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
                    <span className="text-xs text-text-tertiary">
                      留空自动生成
                    </span>
                  </div>
                  <Input
                    aria-describedby={
                      fieldErrors.slug ? slugErrorId : undefined
                    }
                    autoComplete="off"
                    disabled={busy}
                    error={Boolean(fieldErrors.slug)}
                    id={slugId}
                    maxLength={SKILL_SLUG_MAX_LENGTH}
                    onChange={(event) => {
                      setSlug(event.target.value);
                      clearFieldError("slug");
                      setRequestError(null);
                      rotateIdempotencyKey();
                    }}
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
                    onChange={(event) => {
                      setVersionLabel(event.target.value);
                      clearFieldError("label");
                      setRequestError(null);
                      rotateIdempotencyKey();
                    }}
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
                onChange={(event) => {
                  setDescription(event.target.value);
                  clearFieldError("description");
                  setRequestError(null);
                  rotateIdempotencyKey();
                }}
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
                        <select
                          aria-label="发布范围"
                          className="h-9 w-full rounded-lg border border-border-default bg-canvas px-3 text-sm text-text-primary shadow-xs focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus disabled:cursor-not-allowed disabled:opacity-60"
                          disabled={busy}
                          id={scopeId}
                          onChange={(event) => {
                            const nextScope = event.target.value as
                              | "company"
                              | "department";
                            setScope(nextScope);
                            setVisibilityGrantsTouched(false);
                            clearFieldError("ownerDepartmentId");
                            setRequestError(null);
                            rotateIdempotencyKey();
                          }}
                          value={scope}
                        >
                          <option value="company">全公司</option>
                          <option value="department">部门共享</option>
                        </select>
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
                      ) : deptAdmin ? (
                        <select
                          aria-describedby={
                            fieldErrors.ownerDepartmentId
                              ? departmentErrorId
                              : undefined
                          }
                          aria-invalid={
                            fieldErrors.ownerDepartmentId ? true : undefined
                          }
                          aria-label="归属部门"
                          className={cn(
                            "h-9 w-full rounded-lg border bg-canvas px-3 text-sm text-text-primary shadow-xs focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus disabled:cursor-not-allowed disabled:opacity-60",
                            fieldErrors.ownerDepartmentId
                              ? "border-danger"
                              : "border-border-default",
                          )}
                          disabled={busy}
                          id={departmentIdInput}
                          onChange={(event) => {
                            const nextDeptId = event.target.value;
                            setOwnerDepartmentId(nextDeptId);
                            setVisibilityGrants(
                              nextDeptId
                                ? [
                                    defaultOwnerGrant(
                                      nextDeptId,
                                      defaultOwnerIncludeSub,
                                    ),
                                  ]
                                : [],
                            );
                            setVisibilityGrantsTouched(false);
                            clearFieldError("ownerDepartmentId");
                            setRequestError(null);
                            rotateIdempotencyKey();
                          }}
                          value={ownerDepartmentId}
                        >
                          <option value="">请选择归属部门</option>
                          {managedDeptOptions.map((dept) => (
                            <option key={dept.id} value={dept.id}>
                              {dept.name}
                            </option>
                          ))}
                        </select>
                      ) : orgDepartments.isPending ? (
                        <div
                          className="flex h-9 items-center rounded-lg border border-border-subtle bg-canvas px-3 text-xs text-text-tertiary"
                          role="status"
                        >
                          正在加载企业活跃部门列表…
                        </div>
                      ) : orgDepartments.isError ? (
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
                            onChange={(event) => {
                              const nextDeptId = event.target.value;
                              setOwnerDepartmentId(nextDeptId);
                              setVisibilityGrants(
                                nextDeptId.trim()
                                  ? [
                                      defaultOwnerGrant(
                                        nextDeptId.trim(),
                                        defaultOwnerIncludeSub,
                                      ),
                                    ]
                                  : [],
                              );
                              setVisibilityGrantsTouched(false);
                              clearFieldError("ownerDepartmentId");
                              setRequestError(null);
                              rotateIdempotencyKey();
                            }}
                            placeholder="请输入归属部门 ID（如 dept_fin）"
                            value={ownerDepartmentId}
                          />
                          <p className="text-[11px] text-text-tertiary">
                            部门目录加载失败，已降级为手动输入部门 ID。
                          </p>
                        </div>
                      ) : (
                        <select
                          aria-describedby={
                            fieldErrors.ownerDepartmentId
                              ? departmentErrorId
                              : undefined
                          }
                          aria-invalid={
                            fieldErrors.ownerDepartmentId ? true : undefined
                          }
                          aria-label="归属部门"
                          className={cn(
                            "h-9 w-full rounded-lg border bg-canvas px-3 text-sm text-text-primary shadow-xs focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus disabled:cursor-not-allowed disabled:opacity-60",
                            fieldErrors.ownerDepartmentId
                              ? "border-danger"
                              : "border-border-default",
                          )}
                          disabled={busy}
                          id={departmentIdInput}
                          onChange={(event) => {
                            const nextDeptId = event.target.value;
                            setOwnerDepartmentId(nextDeptId);
                            setVisibilityGrants(
                              nextDeptId
                                ? [
                                    defaultOwnerGrant(
                                      nextDeptId,
                                      defaultOwnerIncludeSub,
                                    ),
                                  ]
                                : [],
                            );
                            setVisibilityGrantsTouched(false);
                            clearFieldError("ownerDepartmentId");
                            setRequestError(null);
                            rotateIdempotencyKey();
                          }}
                          value={ownerDepartmentId}
                        >
                          <option value="">请选择归属部门</option>
                          {activeOrgDepartments.map((dept) => (
                            <option key={dept.id} value={dept.id}>
                              {dept.name}
                            </option>
                          ))}
                        </select>
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
                    departmentsError={
                      superAdmin ? orgDepartments.isError : false
                    }
                    departmentsLoading={
                      superAdmin ? orgDepartments.isPending : false
                    }
                    disabled={busy}
                    enabled={isDialogOpen}
                    onChange={(nextGrants) => {
                      setVisibilityGrants(nextGrants);
                      setVisibilityGrantsTouched(true);
                      setRequestError(null);
                      rotateIdempotencyKey();
                    }}
                    ownerDepartmentId={ownerDepartmentId}
                    ownerDepartmentName={ownerDepartmentName}
                    resourceLabel="技能"
                    value={visibilityGrants}
                  />
                ) : null}
              </div>
            ) : null}

            {/* 双栏 Markdown 指南编辑器（支持一键导入 .md 服务端解析回填） */}
            <div className="space-y-1.5">
              <label
                className="block text-xs font-semibold text-text-primary"
                htmlFor={guideId}
              >
                技能指南正文（Markdown）
              </label>
              <MarkdownSplitEditor
                disabled={busy}
                error={fieldErrors.guide}
                id={guideId}
                importTriggerLabel="导入 .md"
                importingMarkdown={parsing}
                label="技能指南正文（Markdown）"
                maxLength={PERSONAL_SKILL_GUIDE_MAX_LENGTH}
                minHeightClassName="min-h-[280px]"
                onChange={(nextGuide) => {
                  setGuide(nextGuide);
                  clearFieldError("guide");
                  setRequestError(null);
                  rotateIdempotencyKey();
                }}
                onImportMarkdownFile={(file) =>
                  void handleImportMarkdownFile(file)
                }
                placeholder={
                  "# 角色定位\n\n请描述本技能的职责与工作步骤。\n\n## 输出要求\n- 逐项核对关键信息\n- 使用清晰表格汇总结论"
                }
                rows={12}
                value={guide}
              />

              {importNotice ? (
                <p
                  className="flex items-center gap-1.5 text-xs font-medium text-brand"
                  role="status"
                >
                  <Sparkles aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
                  <span>{importNotice}</span>
                </p>
              ) : null}
              {importError ? (
                <InlineAlert description={importError} variant="danger" />
              ) : null}
            </div>

            {/* 发布与首页快捷应用设置 */}
            {mode === "create" ? (
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
                      onChange={(event) => {
                        setEnabled(event.target.checked);
                        rotateIdempotencyKey();
                      }}
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
                      onCheckedChange={(checked) => {
                        setFeatured(checked);
                        rotateIdempotencyKey();
                      }}
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
                      {starterPromptLength} / {SKILL_STARTER_PROMPT_MAX_LENGTH}{" "}
                      字符
                    </span>
                  </div>
                  <textarea
                    aria-describedby={joinIds(
                      fieldErrors.starterPrompt
                        ? starterPromptErrorId
                        : undefined,
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
                    onChange={(event) => {
                      setStarterPrompt(event.target.value);
                      clearFieldError("starterPrompt");
                      setRequestError(null);
                      rotateIdempotencyKey();
                    }}
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
            ) : (
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
                    onChange={(event) => {
                      setActivate(event.target.checked);
                      rotateIdempotencyKey();
                    }}
                    type="checkbox"
                  />
                  <span>发布后立即设为当前生效版本</span>
                </label>
                <p className="mt-1 pl-6 text-xs text-text-tertiary">
                  取消勾选时仅追加不可变版本历史，当前生效版本保持不变，稍后可在技能详情中手动切换。
                </p>
              </div>
            )}

            {revisionConflict ? (
              <InlineAlert
                description="该技能已在其他窗口被更新（revision 已变更）。请先关闭本窗口并在技能详情中刷新最新内容后再发布新版本，避免覆盖他人变更。"
                title="修订号冲突"
                variant="warning"
              />
            ) : null}

            {requestError ? (
              <InlineAlert description={requestError} variant="danger" />
            ) : null}
          </form>
        </DialogBody>

        <DialogFooter>
          <Button
            disabled={busy}
            onClick={onClose}
            type="button"
            variant="outline"
          >
            取消
          </Button>
          <Button
            disabled={busy || noManagedDepartments}
            form={formId}
            loading={submitting}
            type="submit"
            variant="primary"
          >
            {mode === "create"
              ? enabled
                ? "创建并启用模板技能"
                : "创建模板技能"
              : activate
                ? "发布并设为当前版本"
                : "仅发布新版本"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
