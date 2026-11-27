import { FilePenLine, Sparkles, Trash2, Upload } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import { formatBytes, formatDateTime } from "../../shared/lib/format";
import { newRequestId } from "../../shared/lib/requestId";
import {
  ADMIN_MUTED_CLASS,
  Badge,
  Button,
  CHECK_GRID_CLASS,
  CHECKBOX_INPUT_CLASS,
  CHECKBOX_LABEL_CLASS,
  CONFIRM_COPY_CLASS,
  FIELD_CLASS,
  FIELD_INPUT_CLASS,
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
  toast,
} from "../../shared/ui";
import { MarkdownContent } from "../../shared/ui/MarkdownContent";
import { Modal } from "../../shared/ui/Modal";
import { WorkspaceFilePicker } from "../../shared/ui/WorkspaceFilePicker";
import {
  VisibilityGrantsEditor,
  normalizeVisibilityGrants,
  visibilityGrantsEqual,
} from "../access/VisibilityGrantsEditor";
import { useAuth } from "../auth/AuthProvider";
import { isSuperAdmin, resolveUserDepartmentName } from "../auth/model";
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
  useUploadSkillVersionMutation,
} from "./hooks";
import {
  SKILL_STARTER_PROMPT_MAX_LENGTH,
  dependencySatisfied,
  normalizeStarterPrompt,
  skillFormat,
  skillScope,
  validateSkillZip,
  validateStarterPrompt,
  versionLabel,
  type DependencyCheck,
  type SkillAdminDetails,
  type SkillConfigurationInput,
  type SkillFormState,
  type SkillVersion,
} from "./model";
import { SkillScopeBadge } from "./SkillScopeBadge";

const VERSION_PAGE_SIZE = 20;

interface ConfigurationBaseline {
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

const DETAIL_PANEL_CLASS =
  "min-w-0 rounded-[11px] border border-border-default bg-canvas p-4";
const DETAIL_PANEL_HEADER_CLASS =
  "mb-3 flex flex-col items-start justify-between gap-4 border-b border-border-subtle pb-2.5 min-[721px]:flex-row [&_h3]:text-base [&_h3]:leading-[1.3] [&_h3]:font-semibold [&_h3]:text-text-primary [&_p]:mt-[3px] [&_p]:text-xs [&_p]:leading-[1.6] [&_p]:text-text-tertiary [&_code]:font-mono [&_code]:text-xs [&_code]:[overflow-wrap:anywhere]";
const VERSION_ITEM_LIST_CLASS = "m-0 flex list-none flex-col gap-[7px] p-0";
const VERSION_ITEM_ROW_CLASS =
  "flex min-w-0 items-start justify-between gap-3 rounded-lg border border-border-default bg-canvas px-2.5 py-[9px]";

function DependencyBadge({ check }: { check: DependencyCheck | null }) {
  const satisfied = dependencySatisfied(check);
  return (
    <Badge
      className="rounded-full"
      variant={satisfied ? "success" : "warning"}
    >
      {satisfied ? "依赖满足" : "依赖未满足"}
    </Badge>
  );
}

function DependencyDetails({ check }: { check: DependencyCheck | null }) {
  if (!check || check.items.length === 0) {
    return <p className={ADMIN_MUTED_CLASS}>该版本未声明 Python 依赖。</p>;
  }
  return (
    <div>
      <p className="mb-[9px] text-xs text-text-tertiary">
        Python {check.python_version} · {check.python_implementation} ·
        检查于 {formatDateTime(check.checked_at)}
      </p>
      <ul className={VERSION_ITEM_LIST_CLASS}>
        {check.items.map((item) => (
          <li
            className="grid min-w-0 grid-cols-1 items-start gap-3 rounded-lg border border-border-default bg-canvas px-2.5 py-[9px] min-[721px]:grid-cols-[auto_minmax(0,1fr)_auto] min-[721px]:items-center"
            key={item.requirement}
          >
            <Badge
              className="rounded-full"
              variant={item.satisfied ? "success" : "warning"}
            >
              {item.satisfied ? "已满足" : "缺失或不符"}
            </Badge>
            <code className="font-mono text-xs text-text-primary [overflow-wrap:anywhere]">
              {item.requirement}
            </code>
            <span className="text-xs leading-[1.55] text-text-tertiary [overflow-wrap:anywhere]">
              {item.marker_applies
                ? item.installed_version
                  ? `已安装 ${item.installed_version}`
                  : item.detail_code ?? "未安装"
                : "当前环境 marker 不适用"}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function VersionContent({ version }: { version: SkillVersion }) {
  return (
    <div className="flex min-w-0 flex-col gap-3.5 [&_section>h4]:mb-2 [&_section>h4]:text-[13px] [&_section>h4]:font-bold [&_section>h4]:text-text-primary">
      <div className="flex min-w-0 items-start justify-between gap-4 border-b border-border-subtle pb-[11px]">
        <div className="min-w-0">
          <strong className="text-base leading-[1.3] text-text-primary">
            {versionLabel(version)}
          </strong>
          <p className="mt-[3px] text-xs leading-[1.6] text-text-tertiary [overflow-wrap:anywhere]">
            {version.slug} · SHA-256{" "}
            <code className="[overflow-wrap:anywhere]">
              {version.package_sha256.slice(0, 16)}…
            </code>
          </p>
        </div>
        <DependencyBadge check={version.dependency_check} />
      </div>

      <section aria-labelledby={`guide-${version.id}`}>
        <h4 id={`guide-${version.id}`}>操作指南</h4>
        <div className="max-h-[360px] overflow-auto rounded-[9px] border border-border-default bg-subtle p-3.5">
          {version.guide ? (
            <MarkdownContent text={version.guide} />
          ) : (
            <p className={ADMIN_MUTED_CLASS}>该版本指南正文为空。</p>
          )}
        </div>
      </section>

      {/* 将文件清单、登记脚本与依赖检查编排为紧凑卡片网格，缩短详情区纵向滚动 */}
      <div className="grid min-w-0 grid-cols-1 gap-3 md:grid-cols-2 [&>section]:min-w-0 [&>section]:rounded-[9px] [&>section]:border [&>section]:border-border-default [&>section]:bg-subtle [&>section]:p-3">
        <section aria-labelledby={`files-${version.id}`}>
          <h4 id={`files-${version.id}`}>文件清单</h4>
          <ul className={VERSION_ITEM_LIST_CLASS}>
            {version.files.map((file) => (
              <li className={VERSION_ITEM_ROW_CLASS} key={file.path}>
                <span className="min-w-0">
                  <strong className="block text-xs text-text-primary [overflow-wrap:anywhere]">
                    {file.path}
                  </strong>
                  <small className="text-xs leading-[1.55] text-text-tertiary [overflow-wrap:anywhere]">
                    {file.text ? "UTF-8 文本" : "二进制资源"} ·{" "}
                    {formatBytes(file.size_bytes)}
                  </small>
                </span>
                <code className="max-w-full shrink-0 font-mono text-xs text-text-secondary [overflow-wrap:anywhere]">
                  {file.sha256.slice(0, 12)}…
                </code>
              </li>
            ))}
          </ul>
        </section>

        <section aria-labelledby={`scripts-${version.id}`}>
          <h4 id={`scripts-${version.id}`}>登记脚本</h4>
          {version.scripts.length === 0 ? (
            <p className={ADMIN_MUTED_CLASS}>该版本未登记可执行脚本。</p>
          ) : (
            <ul className={VERSION_ITEM_LIST_CLASS}>
              {version.scripts.map((script) => (
                <li
                  className={cn(VERSION_ITEM_ROW_CLASS, "flex-col")}
                  key={script.path}
                >
                  <strong className="block text-xs text-text-primary [overflow-wrap:anywhere]">
                    {script.label}
                  </strong>
                  <code className="max-w-full shrink-0 font-mono text-xs text-text-secondary [overflow-wrap:anywhere]">
                    {script.path}
                  </code>
                  <p className="mt-0.5 text-xs leading-[1.55] text-text-tertiary [overflow-wrap:anywhere]">
                    {script.purpose}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section
          aria-labelledby={`dependencies-${version.id}`}
          className="md:col-span-2"
        >
          <h4 id={`dependencies-${version.id}`}>依赖检查</h4>
          <DependencyDetails check={version.dependency_check} />
        </section>
      </div>
    </div>
  );
}

function VersionUpload({
  disabled,
  onUploaded,
  skillId,
  slug,
}: {
  disabled: boolean;
  onUploaded: () => void;
  skillId: string;
  slug: string;
}) {
  const inputId = useId();
  const labelId = useId();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const requestIdRef = useRef<string | null>(null);
  const mutation = useUploadSkillVersionMutation(skillId);

  const upload = async () => {
    const validation = validateSkillZip(file);
    if (validation) {
      setError(validation);
      return;
    }
    if (!file || mutation.isPending) {
      return;
    }
    const requestId = requestIdRef.current ?? newRequestId();
    requestIdRef.current = requestId;
    setError(null);
    setNotice(null);
    try {
      const version = await mutation.mutateAsync({
        file,
        label: label.trim() || undefined,
        requestId,
      });
      requestIdRef.current = null;
      setFile(null);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
      setLabel("");
      setNotice(
        `版本 ${versionLabel(version)} 已保存。它尚未自动成为当前版本，请在上方明确选择后保存配置。`,
      );
      onUploaded();
    } catch (failure) {
      setError(skillErrorMessage(failure));
    }
  };

  return (
    <section
      aria-labelledby={`${inputId}-title`}
      className={cn(
        DETAIL_PANEL_CLASS,
        "grid grid-cols-1 items-end gap-x-3 gap-y-[11px] min-[721px]:grid-cols-[minmax(0,1fr)_minmax(220px,0.9fr)_auto]",
      )}
    >
      {/* 标题与说明独占首行，第二行按文件选择器、标签输入、上传按钮三列底部对齐 */}
      <div className="col-span-full self-start">
        <h4
          className="text-base leading-[1.3] font-semibold text-text-primary"
          id={`${inputId}-title`}
        >
          上传完整新版本
        </h4>
        <p className="mt-[3px] text-xs leading-[1.6] text-text-tertiary">
          新包 slug 必须为 {slug}。上传只新增不可变版本，不自动切换。
        </p>
      </div>
      {/* 复用通用文件选择器并启用按钮触发模式，保持顶部标签对齐与跳过隐藏 input 的键盘焦点序 */}
      <div className={FIELD_CLASS}>
        <label htmlFor={inputId}>选择新版本 ZIP</label>
        <WorkspaceFilePicker
          accept=".zip,application/zip"
          buttonTrigger
          disabled={disabled || mutation.isPending}
          file={file}
          id={inputId}
          inputRef={fileInputRef}
          label="选择新版本 ZIP"
          onChange={(selected) => {
            setFile(selected);
            setError(null);
            setNotice(null);
            requestIdRef.current = null;
          }}
          placeholder="未选择 .zip 文件"
          triggerLabel="选择 ZIP 文件"
        />
      </div>
      <label className={FIELD_CLASS} htmlFor={labelId}>
        <span>版本标签（可选）</span>
        <input
          className={cn(FIELD_INPUT_CLASS, "min-h-[38px]")}
          disabled={disabled || mutation.isPending}
          id={labelId}
          maxLength={64}
          onChange={(event) => {
            setLabel(event.target.value);
            setError(null);
            setNotice(null);
            requestIdRef.current = null;
          }}
          placeholder="例如：2026 年 11 月模板"
          value={label}
        />
      </label>
      <Button
        className="h-[38px]"
        disabled={disabled || mutation.isPending || !file}
        onClick={() => void upload()}
        type="button"
        variant="outline"
      >
        <Upload
          aria-hidden="true"
          focusable="false"
          size={14}
          strokeWidth={1.7}
        />
        {mutation.isPending ? "正在检查并上传…" : "上传新版本"}
      </Button>
      {notice ? (
        <p
          className={cn(
            NOTICE_SUCCESS_CLASS,
            "col-span-full min-w-0 [overflow-wrap:anywhere]",
          )}
          role="status"
        >
          {notice}
        </p>
      ) : null}
      {error ? (
        <div className="col-span-full min-w-0">
          <InlineAlert size="compact" variant="danger">
            {error}
          </InlineAlert>
        </div>
      ) : null}
    </section>
  );
}

interface AdminSkillDetailDialogProps {
  onClose: () => void;
  /** 由父页面安全关闭旧 <Modal> 详情窗后受控切换打开 Radix 在线发版 Dialog，严禁在旧 Modal 上叠开低层 Dialog。 */
  onEditMarkdownVersion?: (skill: SkillAdminDetails) => void;
  open: boolean;
  skillId: string | null;
}

export function AdminSkillDetailDialog({
  onClose,
  onEditMarkdownVersion,
  open,
  skillId,
}: AdminSkillDetailDialogProps) {
  const { user } = useAuth();
  const superAdmin = isSuperAdmin(user);
  const versionSelectId = useId();
  const featuredSwitchId = useId();
  const starterPromptId = useId();
  const starterPromptErrorId = useId();
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
  const submitLock = useRef(false);

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
    submitLock.current = false;
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

  const close = () => {
    if (
      !update.isPending &&
      !deleteSkillMutation.isPending &&
      !submitLock.current
    ) {
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
      submitLock.current ||
      update.isPending
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

    const nextStarterPrompt = normalizeStarterPrompt(form.starterPrompt);
    const baselineStarterPrompt = normalizeStarterPrompt(
      baseline.form.starterPrompt,
    );
    // 仅在新增配置字段相对基线发生变化时显式携带，未改动时省略以保持向后兼容与契约 3.7 语义。
    const featuredChanged = form.featured !== baseline.form.featured;
    const starterPromptChanged = nextStarterPrompt !== baselineStarterPrompt;
    const grantsChanged =
      skillScope(detail.data) === "department" &&
      Boolean(detail.data.owner_department_id) &&
      !visibilityGrantsEqual(
        form.visibilityGrants,
        baseline.form.visibilityGrants,
      );

    const payload: SkillConfigurationInput = {
      current_version_id: form.currentVersionId,
      enabled: form.enabled,
      expected_revision: baseline.revision,
      script_requires_approval: form.scriptRequiresApproval,
      ...(featuredChanged ? { featured: form.featured } : {}),
      ...(starterPromptChanged ? { starter_prompt: nextStarterPrompt } : {}),
      ...(grantsChanged ? { visibility_grants: form.visibilityGrants } : {}),
    };

    submitLock.current = true;
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
    } finally {
      submitLock.current = false;
    }
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
      deleteSkillMutation.isPending
    ) {
      return;
    }
    setDeleteError(null);
    try {
      await deleteSkillMutation.mutateAsync({
        expectedRevision: baseline.revision,
        skillId: detail.data.id,
      });
      setDeleteDialogOpen(false);
      toast.success(`部门技能「${detail.data.name}」已删除`);
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
  };

  const pending = update.isPending || deleteSkillMutation.isPending;

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
            {update.isPending ? "正在保存配置…" : "保存当前配置"}
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

          <section
            aria-labelledby="skill-versions-title"
            className={DETAIL_PANEL_CLASS}
          >
            <header className={DETAIL_PANEL_HEADER_CLASS}>
              <div>
                <h3 id="skill-versions-title">不可变版本历史</h3>
                <p>选择上方表单中的版本可预览对应指南、资源与检查结果。</p>
              </div>
            </header>
            {/* 桌面端采用 Master-Detail 左右分栏：左侧分页与版本列表，右侧版本详情 */}
            <div className="grid min-w-0 grid-cols-1 items-start gap-4 md:grid-cols-[minmax(260px,276px)_minmax(0,1fr)]">
              <div className="flex min-w-0 flex-col gap-2.5">
                <div
                  aria-label="版本历史分页"
                  className="flex flex-col flex-wrap items-stretch gap-x-2 gap-y-1.5 text-xs text-text-tertiary min-[721px]:flex-row min-[721px]:items-center"
                >
                  <span className="min-[721px]:basis-full">
                    共 {versionPage.data?.total ?? versions.length} 个版本
                  </span>
                  <Button
                    disabled={
                      versionPage.isFetching || versionPageNumber <= 1
                    }
                    onClick={() =>
                      setVersionPageNumber((current) =>
                        Math.max(1, current - 1),
                      )
                    }
                    size="xs"
                    type="button"
                    variant="outline"
                  >
                    上一页版本
                  </Button>
                  <span>
                    第 {versionPage.data?.page ?? versionPageNumber} /{" "}
                    {Math.max(
                      1,
                      Math.ceil(
                        (versionPage.data?.total ?? 0) /
                          VERSION_PAGE_SIZE,
                      ),
                    )}{" "}
                    页
                  </span>
                  <Button
                    disabled={
                      versionPage.isFetching ||
                      !versionPage.data ||
                      versionPageNumber >=
                        Math.ceil(
                          versionPage.data.total / VERSION_PAGE_SIZE,
                        )
                    }
                    onClick={() =>
                      setVersionPageNumber((current) => current + 1)
                    }
                    size="xs"
                    type="button"
                    variant="outline"
                  >
                    下一页版本
                  </Button>
                </div>
                {versionPage.isPending && versions.length === 0 ? (
                  <p className={ADMIN_MUTED_CLASS} role="status">
                    正在读取不可变版本历史…
                  </p>
                ) : versionPage.isError ? (
                  <InlineAlert
                    action={
                      <button
                        className={TEXT_BUTTON_CLASS}
                        onClick={() => void versionPage.refetch()}
                        type="button"
                      >
                        重试
                      </button>
                    }
                    size="compact"
                    variant="danger"
                  >
                    版本历史分页加载失败；已保留服务端详情中的版本。
                  </InlineAlert>
                ) : versions.length === 0 ? (
                  <p className={ADMIN_MUTED_CLASS}>该技能尚无不可变版本。</p>
                ) : null}
                <ol className="m-0 flex max-h-[260px] min-w-0 list-none flex-col gap-[7px] overflow-y-auto pr-0.5 pl-0 md:max-h-[520px]">
                  {versions.map((version) => {
                    const isSelected = version.id === form.currentVersionId;
                    return (
                      <li className="shrink-0" key={version.id}>
                        <button
                          className={cn(
                            "flex w-full min-w-0 items-start justify-between gap-2 rounded-[9px] border px-[11px] py-2.5 text-left transition-colors hover:border-brand-border hover:bg-brand-subtle",
                            isSelected
                              ? "border-brand-border bg-brand-subtle shadow-[inset_3px_0_0_var(--brand-hover)]"
                              : "border-border-default bg-canvas",
                          )}
                          disabled={pending}
                          onClick={() => {
                            setForm({
                              ...form,
                              currentVersionId: version.id,
                            });
                            setRequestError(null);
                            setNotice(null);
                          }}
                          type="button"
                        >
                          <span className="flex min-w-0 flex-col gap-[3px]">
                            <strong className="text-xs text-text-primary [overflow-wrap:anywhere]">
                              {versionLabel(version)}
                            </strong>
                            <small className="text-xs text-text-tertiary [overflow-wrap:anywhere]">
                              {formatDateTime(version.created_at)} ·{" "}
                              {version.files.length} 个文件
                            </small>
                          </span>
                          <DependencyBadge check={version.dependency_check} />
                        </button>
                      </li>
                    );
                  })}
                </ol>
              </div>
              <div className="min-w-0 border-t border-border-subtle pt-3.5 md:border-t-0 md:border-l md:pt-0 md:pl-4">
                {selectedVersion ? (
                  <VersionContent version={selectedVersion} />
                ) : versionDetailPending ? (
                  <p className={ADMIN_MUTED_CLASS} role="status">
                    正在读取所选版本的指南、资源和依赖检查…
                  </p>
                ) : (
                  <p className={ADMIN_MUTED_CLASS}>请选择一个版本查看内容。</p>
                )}
              </div>
            </div>
          </section>
        </div>
      ) : null}

      {/* 复用支持 ModalDepthContext 层级栈的嵌套子 Modal，确保确认框稳定位于详情弹窗之上且 Escape/焦点隔离正常。 */}
      {isDepartmentSkill && detail.data ? (
        <Modal
          closeDisabled={deleteSkillMutation.isPending}
          description="删除后后续新对话将不再展示该技能，历史运行记录不受影响。"
          footer={
            <>
              <Button
                disabled={deleteSkillMutation.isPending}
                onClick={() => setDeleteDialogOpen(false)}
                type="button"
                variant="outline"
              >
                取消
              </Button>
              <Button
                disabled={deleteSkillMutation.isPending}
                onClick={() => void handleConfirmDeleteDepartmentSkill()}
                type="button"
                variant="danger"
              >
                {deleteSkillMutation.isPending ? "正在删除…" : "确认删除"}
              </Button>
            </>
          }
          onClose={() => {
            if (!deleteSkillMutation.isPending) {
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
