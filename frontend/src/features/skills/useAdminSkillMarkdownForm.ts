import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";

import { ApiError } from "../../shared/api/client";
import { newRequestId } from "../../shared/lib/requestId";
import { useSubmitLock } from "../../shared/lib/useSubmitLock";
import { toast } from "../../shared/ui";
import {
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
  SKILL_STARTER_PROMPT_MAX_LENGTH,
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

export interface MarkdownFormErrors {
  description?: string;
  guide?: string;
  label?: string;
  name?: string;
  ownerDepartmentId?: string;
  slug?: string;
  starterPrompt?: string;
}

export interface ManagedDepartmentOption {
  id: string;
  name: string;
}

export interface UseAdminSkillMarkdownFormOptions {
  mode: "create" | "version";
  onClose: () => void;
  onSaved: (details: SkillAdminDetails) => void;
  open: boolean;
  returnFocusRef?: RefObject<HTMLElement | null>;
  skill?: SkillAdminDetails | null;
}

/**
 * 管理端在线 Markdown 模板技能双模式表单 Hook：
 * - 承接新建与发布版本双模式的草稿初始化、字段校验与防并发提交锁；
 * - 管理 .md 文件 frontmatter 解析回填与幂等键轮换（失败重试复用、改稿轮换）；
 * - 处理 409 修订号冲突乐观锁提示与受控 Dialog 焦点捕获/归还。
 */
export function useAdminSkillMarkdownForm(
  options: UseAdminSkillMarkdownFormOptions,
) {
  const { mode, onClose, onSaved, open, returnFocusRef } = options;
  const targetSkill = mode === "version" ? (options.skill ?? null) : null;
  const { user } = useAuth();
  const superAdmin = isSuperAdmin(user);
  const deptAdmin = isDeptAdmin(user);
  // 部门管理员归属部门默认行使用 include_subdepartments=false，避免直属管辖部门管理员误触 403；
  // 超级管理员默认 true。
  const defaultOwnerIncludeSub = superAdmin;

  const nameInputRef = useRef<HTMLInputElement>(null);
  const requestIdRef = useRef<string | null>(null);
  const submitLock = useSubmitLock();
  // 受控 Radix Dialog 无 DialogTrigger：打开瞬间同步捕获原焦点元素，关闭时在 onCloseAutoFocus 归还焦点。
  const returnFocusElementRef = useRef<HTMLElement | null>(null);
  const focusCaptureWasOpenRef = useRef(false);
  const initWasOpenRef = useRef(false);

  const managedDeptOptions = useMemo<ManagedDepartmentOption[]>(() => {
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

  // 在 Radix FocusScope 接管焦点前的 layout 阶段捕获触发源，确保关闭时精准归还。
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

  const submitting =
    createMutation.isPending || publishMutation.isPending || submitLock.isLocked;
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
    if (!nextOpen && !busy) {
      onClose();
    }
  };

  const handleCloseAutoFocus = (event: Event) => {
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
  };

  const handleOpenAutoFocus = (event: Event) => {
    event.preventDefault();
    nameInputRef.current?.focus();
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
    if (busy || noManagedDepartments) {
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

    // 同步提交锁防止在 mutation isPending 生效前双击连发重复请求。
    await submitLock.runLocked(async () => {
      const requestId = requestIdRef.current ?? newRequestId();
      requestIdRef.current = requestId;

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
            setFieldErrors((current) => ({
              ...current,
              starterPrompt: message,
            }));
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
      }
    });
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

  const handleNameChange = (nextName: string) => {
    setName(nextName);
    clearFieldError("name");
    setRequestError(null);
    rotateIdempotencyKey();
  };

  const handleSlugChange = (nextSlug: string) => {
    setSlug(nextSlug);
    clearFieldError("slug");
    setRequestError(null);
    rotateIdempotencyKey();
  };

  const handleVersionLabelChange = (nextLabel: string) => {
    setVersionLabel(nextLabel);
    clearFieldError("label");
    setRequestError(null);
    rotateIdempotencyKey();
  };

  const handleDescriptionChange = (nextDescription: string) => {
    setDescription(nextDescription);
    clearFieldError("description");
    setRequestError(null);
    rotateIdempotencyKey();
  };

  const handleScopeChange = (nextScope: "company" | "department") => {
    setScope(nextScope);
    setVisibilityGrantsTouched(false);
    clearFieldError("ownerDepartmentId");
    setRequestError(null);
    rotateIdempotencyKey();
  };

  const handleOwnerDepartmentChange = (
    nextDeptId: string,
    trimForGrant = false,
  ) => {
    setOwnerDepartmentId(nextDeptId);
    const grantDeptId = trimForGrant ? nextDeptId.trim() : nextDeptId;
    setVisibilityGrants(
      grantDeptId
        ? [defaultOwnerGrant(grantDeptId, defaultOwnerIncludeSub)]
        : [],
    );
    setVisibilityGrantsTouched(false);
    clearFieldError("ownerDepartmentId");
    setRequestError(null);
    rotateIdempotencyKey();
  };

  const handleVisibilityGrantsChange = (nextGrants: VisibilityGrantInput[]) => {
    setVisibilityGrants(nextGrants);
    setVisibilityGrantsTouched(true);
    setRequestError(null);
    rotateIdempotencyKey();
  };

  const handleGuideChange = (nextGuide: string) => {
    setGuide(nextGuide);
    clearFieldError("guide");
    setRequestError(null);
    rotateIdempotencyKey();
  };

  const handleEnabledChange = (checked: boolean) => {
    setEnabled(checked);
    rotateIdempotencyKey();
  };

  const handleFeaturedChange = (checked: boolean) => {
    setFeatured(checked);
    rotateIdempotencyKey();
  };

  const handleStarterPromptChange = (nextPrompt: string) => {
    setStarterPrompt(nextPrompt);
    clearFieldError("starterPrompt");
    setRequestError(null);
    rotateIdempotencyKey();
  };

  const handleActivateChange = (checked: boolean) => {
    setActivate(checked);
    rotateIdempotencyKey();
  };

  return {
    activate,
    activeOrgDepartments,
    baselineRevision,
    busy,
    defaultOwnerIncludeSub,
    deptAdmin,
    description,
    descriptionLength,
    enabled,
    featured,
    fieldErrors,
    guide,
    handleActivateChange,
    handleCloseAutoFocus,
    handleDescriptionChange,
    handleEnabledChange,
    handleFeaturedChange,
    handleGuideChange,
    handleImportMarkdownFile,
    handleNameChange,
    handleOpenAutoFocus,
    handleOpenChange,
    handleOwnerDepartmentChange,
    handleScopeChange,
    handleSlugChange,
    handleStarterPromptChange,
    handleSubmit,
    handleVersionLabelChange,
    handleVisibilityGrantsChange,
    hasDepartmentControl,
    importError,
    importNotice,
    isDialogOpen,
    managedDeptOptions,
    name,
    nameInputRef,
    nameLength,
    noManagedDepartments,
    orgDepartmentsError: orgDepartments.isError,
    orgDepartmentsPending: orgDepartments.isPending,
    ownerDepartmentId,
    ownerDepartmentName,
    parsing,
    requestError,
    revisionConflict,
    scope,
    slug,
    starterPrompt,
    starterPromptLength,
    starterPromptOverLimit,
    submitting,
    superAdmin,
    targetSkill,
    versionLabel,
    visibilityGrants,
  };
}
