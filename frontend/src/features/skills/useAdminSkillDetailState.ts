import { useEffect, useMemo, useState } from "react";

import { useSubmitLock } from "../../shared/lib/useSubmitLock";
import { toast } from "../../shared/ui";
import {
  normalizeVisibilityGrants,
  visibilityGrantsEqual,
} from "../access/VisibilityGrantsEditor";
import { useAuth } from "../auth/AuthProvider";
import { isSuperAdmin, resolveUserDepartmentName } from "../auth/model";
import { VERSION_PAGE_SIZE } from "./AdminSkillVersionPanel";
import {
  isSkillRevisionConflict,
  skillErrorMessage,
} from "./errorMessages";
import {
  useAdminSkill,
  useAdminSkillVersion,
  useAdminSkillVersions,
  useDeleteAdminSkillMutation,
  useUpdateSkillConfigurationMutation,
} from "./hooks";
import {
  SKILL_STARTER_PROMPT_MAX_LENGTH,
  dependencySatisfied,
  normalizeStarterPrompt,
  skillFormat,
  skillScope,
  validateStarterPrompt,
  versionLabel,
  type SkillAdminDetails,
  type SkillConfigurationInput,
  type SkillFormState,
  type SkillVersion,
} from "./model";

export interface ConfigurationBaseline {
  form: SkillFormState;
  revision: number;
  skillId: string;
}

function formFromSkill(
  skill: SkillAdminDetails,
  defaultIncludeSubdepartments: boolean,
): SkillFormState {
  const ownerDeptId = skill.owner_department_id?.trim() ?? "";
  return {
    currentVersionId: skill.current_version_id ?? "",
    enabled: skill.enabled,
    featured: skill.featured,
    scriptRequiresApproval: skill.script_requires_approval,
    starterPrompt: skill.starter_prompt ?? "",
    visibilityGrants:
      skillScope(skill) === "department" && ownerDeptId
        ? normalizeVisibilityGrants(
            ownerDeptId,
            skill.visibility_grants,
            defaultIncludeSubdepartments,
          )
        : [],
  };
}

function formsEqual(
  left: SkillFormState | null,
  right: SkillFormState | null,
): boolean {
  return Boolean(
    left &&
      right &&
      left.currentVersionId === right.currentVersionId &&
      left.enabled === right.enabled &&
      left.featured === right.featured &&
      left.scriptRequiresApproval === right.scriptRequiresApproval &&
      normalizeStarterPrompt(left.starterPrompt) ===
        normalizeStarterPrompt(right.starterPrompt) &&
      visibilityGrantsEqual(left.visibilityGrants, right.visibilityGrants),
  );
}

function mergeVersions(
  ...groups: Array<readonly SkillVersion[]>
): SkillVersion[] {
  return Array.from(
    new Map(
      groups
        .flat()
        .map((version) => [version.id, version] as const),
    ).values(),
  ).sort((left, right) => right.ordinal - left.ordinal);
}

interface UseAdminSkillDetailStateOptions {
  onClose: () => void;
  open: boolean;
  skillId: string | null;
}

/**
 * 技能详情与发布配置状态 Hook：
 * - 承接详情查询、双源版本分页与列表外版本单条补拉合并；
 * - 维护服务端乐观锁配置基线，后台刷新仅标冲突而不覆盖未保存草稿；
 * - 复用 useSubmitLock 保护配置保存与部门技能删除，防止同一 render tick 内双击连发。
 */
export function useAdminSkillDetailState({
  onClose,
  open,
  skillId,
}: UseAdminSkillDetailStateOptions) {
  const { user } = useAuth();
  const superAdmin = isSuperAdmin(user);
  const activeSkillId = open ? skillId : null;
  const detail = useAdminSkill(activeSkillId);
  const [versionPageNumber, setVersionPageNumber] = useState(1);
  const versionPage = useAdminSkillVersions(
    activeSkillId,
    versionPageNumber,
    VERSION_PAGE_SIZE,
  );
  const update = useUpdateSkillConfigurationMutation(skillId ?? "");
  const deleteSkillMutation = useDeleteAdminSkillMutation();
  const saveLock = useSubmitLock();
  const deleteLock = useSubmitLock();

  const [form, setForm] = useState<SkillFormState | null>(null);
  // revision 是服务端乐观锁基线；后台刷新只标记冲突，不能覆盖未保存草稿。
  const [baseline, setBaseline] =
    useState<ConfigurationBaseline | null>(null);
  const [revisionConflict, setRevisionConflict] = useState(false);
  const [conflictMessage, setConflictMessage] = useState<string | null>(
    null,
  );
  const [starterPromptError, setStarterPromptError] = useState<string | null>(
    null,
  );
  const [requestError, setRequestError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [versionSelectOpen, setVersionSelectOpen] = useState(false);

  useEffect(() => {
    setForm(null);
    setBaseline(null);
    setRevisionConflict(false);
    setConflictMessage(null);
    setStarterPromptError(null);
    setRequestError(null);
    setNotice(null);
    setDeleteDialogOpen(false);
    setDeleteError(null);
    setVersionSelectOpen(false);
    setVersionPageNumber(1);
  }, [open, skillId]);

  useEffect(() => {
    if (!open || !detail.data) {
      return;
    }
    if (baseline?.skillId === detail.data.id) {
      return;
    }
    const skill = detail.data;
    const nextForm = formFromSkill(skill, superAdmin);
    setBaseline({
      form: nextForm,
      revision: skill.revision,
      skillId: skill.id,
    });
    setForm(nextForm);
  }, [baseline?.skillId, detail.data, open, superAdmin]);

  // 详情接口只含最近版本，分页结果才是历史列表；列表外的当前/选中版本单独补取。
  const knownVersions = useMemo(
    () =>
      versionPage.data?.items ??
      detail.data?.versions ??
      [],
    [detail.data?.versions, versionPage.data?.items],
  );
  const currentVersionId = detail.data?.current_version_id ?? null;
  const currentVersionOutsideKnownList = Boolean(
    currentVersionId &&
      !knownVersions.some((version) => version.id === currentVersionId),
  );
  const currentVersionDetail = useAdminSkillVersion(
    activeSkillId,
    currentVersionOutsideKnownList ? currentVersionId : null,
  );
  const selectedVersionId = form?.currentVersionId || null;
  const selectedVersionNeedsDetail = Boolean(
    selectedVersionId &&
      selectedVersionId !==
        (currentVersionOutsideKnownList ? currentVersionId : null) &&
      !knownVersions.some(
        (version) => version.id === selectedVersionId,
      ),
  );
  const selectedVersionDetail = useAdminSkillVersion(
    activeSkillId,
    selectedVersionNeedsDetail ? selectedVersionId : null,
  );
  const versions = useMemo(
    () =>
      mergeVersions(
        knownVersions,
        currentVersionDetail.data ? [currentVersionDetail.data] : [],
        selectedVersionDetail.data ? [selectedVersionDetail.data] : [],
      ),
    [
      currentVersionDetail.data,
      knownVersions,
      selectedVersionDetail.data,
    ],
  );

  const saving = update.isPending || saveLock.isLocked;
  const deleting = deleteSkillMutation.isPending || deleteLock.isLocked;
  const pending = saving || deleting;

  const close = () => {
    if (!pending) {
      onClose();
    }
  };

  const selectedVersion = versions.find(
    (version) => version.id === form?.currentVersionId,
  );
  const selectedDependencySatisfied = dependencySatisfied(
    selectedVersion?.dependency_check ??
      detail.data?.dependency_check,
  );
  const canEnable = selectedDependencySatisfied;
  const isDirty = Boolean(
    form && baseline && !formsEqual(form, baseline.form),
  );
  const isVersionDirty = Boolean(
    form &&
      baseline &&
      form.currentVersionId !== baseline.form.currentVersionId,
  );
  const serverRevisionChanged = Boolean(
    baseline &&
      detail.data &&
      baseline.skillId === detail.data.id &&
      baseline.revision !== detail.data.revision,
  );
  const hasRevisionConflict =
    serverRevisionChanged || revisionConflict;
  const versionDetailPending =
    (currentVersionOutsideKnownList &&
      currentVersionDetail.isPending) ||
    (selectedVersionNeedsDetail && selectedVersionDetail.isPending);

  const acceptServerDetails = (skill: SkillAdminDetails) => {
    const nextForm = formFromSkill(skill, superAdmin);
    setBaseline({
      form: nextForm,
      revision: skill.revision,
      skillId: skill.id,
    });
    setForm(nextForm);
    setStarterPromptError(null);
    setRevisionConflict(false);
    setConflictMessage(null);
  };

  const reloadLatestConfiguration = async () => {
    setRequestError(null);
    setNotice(null);
    try {
      const refreshed = await detail.refetch();
      if (!refreshed.isSuccess || !refreshed.data) {
        setConflictMessage(
          "暂时无法重新读取服务端最新配置，当前草稿仍已保留。",
        );
        return;
      }
      acceptServerDetails(refreshed.data);
      setVersionPageNumber(1);
      setNotice("已重新载入服务端最新配置。");
    } catch {
      setConflictMessage(
        "暂时无法重新读取服务端最新配置，当前草稿仍已保留。",
      );
    }
  };

  const save = async () => {
    if (
      !detail.data ||
      !baseline ||
      !form ||
      hasRevisionConflict ||
      pending
    ) {
      return;
    }
    if (!form.currentVersionId) {
      setRequestError("请先选择一个不可变技能版本。");
      return;
    }
    if (form.enabled && !canEnable) {
      setRequestError("所选版本依赖未满足，不能启用。");
      return;
    }
    const promptValidationError = validateStarterPrompt(form.starterPrompt);
    setStarterPromptError(promptValidationError);
    if (promptValidationError) {
      return;
    }

    const currentDetail = detail.data;
    const currentBaseline = baseline;
    const currentForm = form;
    const nextStarterPrompt = normalizeStarterPrompt(currentForm.starterPrompt);
    const baselineStarterPrompt = normalizeStarterPrompt(
      currentBaseline.form.starterPrompt,
    );
    // 仅在新增配置字段相对基线发生变化时显式携带，未改动时省略以保持向后兼容与契约 3.7 语义。
    const featuredChanged =
      currentForm.featured !== currentBaseline.form.featured;
    const starterPromptChanged = nextStarterPrompt !== baselineStarterPrompt;
    const grantsChanged =
      skillScope(currentDetail) === "department" &&
      Boolean(currentDetail.owner_department_id) &&
      !visibilityGrantsEqual(
        currentForm.visibilityGrants,
        currentBaseline.form.visibilityGrants,
      );

    const payload: SkillConfigurationInput = {
      current_version_id: currentForm.currentVersionId,
      enabled: currentForm.enabled,
      expected_revision: currentBaseline.revision,
      script_requires_approval: currentForm.scriptRequiresApproval,
      ...(featuredChanged ? { featured: currentForm.featured } : {}),
      ...(starterPromptChanged ? { starter_prompt: nextStarterPrompt } : {}),
      ...(grantsChanged
        ? { visibility_grants: currentForm.visibilityGrants }
        : {}),
    };

    await saveLock.runLocked(async () => {
      setRequestError(null);
      setNotice(null);
      try {
        const saved = await update.mutateAsync(payload);
        acceptServerDetails(saved);
        setNotice("技能配置已保存；新 Run 才会使用本次当前版本与审批策略。");
      } catch (error) {
        if (isSkillRevisionConflict(error)) {
          // 409 后只刷新服务端基线并保留本地草稿，必须由管理员显式重新载入。
          setRevisionConflict(true);
          setConflictMessage(null);
          try {
            const refreshed = await detail.refetch();
            if (!refreshed.isSuccess) {
              setConflictMessage(
                "技能配置已被并发修改，且暂时无法读取最新详情；当前草稿仍已保留。",
              );
            }
          } catch {
            setConflictMessage(
              "技能配置已被并发修改，且暂时无法读取最新详情；当前草稿仍已保留。",
            );
          }
        } else {
          setRequestError(skillErrorMessage(error));
        }
      }
    });
  };

  const currentScope = detail.data ? skillScope(detail.data) : null;
  const currentFormat = detail.data ? skillFormat(detail.data) : null;
  // 双向格式锁：format === "markdown" 为在线模板，隐藏 ZIP 上传并开放 Markdown 在线发版；
  // format === "package" 为 ZIP 技能包，保留 ZIP 上传并隐藏在线 Markdown 编辑器。
  const isMarkdownSkill = currentFormat === "markdown";
  // 严格遵守后端契约：仅部门级技能允许软删除，全公司（company）技能绝不渲染删除入口。
  const isDepartmentSkill = currentScope === "department";
  const currentDepartmentName = detail.data
    ? (resolveUserDepartmentName(user, detail.data.owner_department_id) ??
      detail.data.owner_department_id ??
      null)
    : null;
  const starterPromptLength = form
    ? Array.from(form.starterPrompt.trim()).length
    : 0;
  const starterPromptOverLimit =
    starterPromptLength > SKILL_STARTER_PROMPT_MAX_LENGTH;

  const handleConfirmDeleteDepartmentSkill = async () => {
    if (
      !detail.data ||
      !baseline ||
      !isDepartmentSkill ||
      pending
    ) {
      return;
    }
    const targetSkill = detail.data;
    const expectedRevision = baseline.revision;
    await deleteLock.runLocked(async () => {
      setDeleteError(null);
      try {
        await deleteSkillMutation.mutateAsync({
          expectedRevision,
          skillId: targetSkill.id,
        });
        setDeleteDialogOpen(false);
        toast.success(`部门技能「${targetSkill.name}」已删除`);
        onClose();
      } catch (error) {
        if (isSkillRevisionConflict(error)) {
          setRevisionConflict(true);
          setConflictMessage(null);
          setDeleteError(
            "该部门技能已被并发更新（修订号冲突），请先关闭确认框并重新载入最新配置后再决定是否删除。",
          );
          void detail.refetch();
        } else {
          setDeleteError(skillErrorMessage(error));
        }
      }
    });
  };

  useEffect(() => {
    if (pending) {
      // 保存或删除提交期间禁用版本选择器，同时立即收起已展开的下拉菜单。
      setVersionSelectOpen(false);
    }
  }, [pending]);

  const versionOptions = useMemo(() => {
    if (!detail.data || !form) {
      return [];
    }
    return [
      ...(!selectedVersion && form.currentVersionId
        ? [
            {
              badge: selectedDependencySatisfied ? undefined : "依赖未满足",
              label: `${
                detail.data.current_version_label ?? form.currentVersionId
              } · 正在读取版本详情`,
              value: form.currentVersionId,
            },
          ]
        : []),
      ...versions.map((version) => ({
        badge: dependencySatisfied(version.dependency_check)
          ? undefined
          : "依赖未满足",
        label: versionLabel(version),
        value: version.id,
      })),
    ];
  }, [
    detail.data,
    form,
    selectedDependencySatisfied,
    selectedVersion,
    versions,
  ]);

  const selectedVersionOptionLabel = form?.currentVersionId
    ? versionOptions.find((option) => option.value === form.currentVersionId)
        ?.label
    : undefined;

  return {
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
    selectedDependencySatisfied,
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
  };
}
