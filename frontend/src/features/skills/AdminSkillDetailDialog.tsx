import { useEffect, useId, useMemo, useRef, useState } from "react";

import { formatBytes, formatDateTime } from "../../shared/lib/format";
import { newRequestId } from "../../shared/lib/requestId";
import { InlineAlert, toast } from "../../shared/ui";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { MarkdownContent } from "../../shared/ui/MarkdownContent";
import { Modal } from "../../shared/ui/Modal";
import { SelectDropdown } from "../../shared/ui/SelectDropdown";
import { WorkspaceFilePicker } from "../../shared/ui/WorkspaceFilePicker";
import { useAuth } from "../auth/AuthProvider";
import { resolveUserDepartmentName } from "../auth/model";
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
  dependencySatisfied,
  skillScope,
  validateSkillZip,
  versionLabel,
  type DependencyCheck,
  type SkillAdminDetails,
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

function formFromSkill(skill: SkillAdminDetails): SkillFormState {
  return {
    currentVersionId: skill.current_version_id ?? "",
    enabled: skill.enabled,
    scriptRequiresApproval: skill.script_requires_approval,
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
      left.scriptRequiresApproval === right.scriptRequiresApproval,
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

function DependencyBadge({ check }: { check: DependencyCheck | null }) {
  const satisfied = dependencySatisfied(check);
  return (
    <span
      className={`admin-status-badge ${
        satisfied ? " is-success" : " is-warning"
      }`}
    >
      {satisfied ? "依赖满足" : "依赖未满足"}
    </span>
  );
}

function DependencyDetails({ check }: { check: DependencyCheck | null }) {
  if (!check || check.items.length === 0) {
    return <p className="admin-muted">该版本未声明 Python 依赖。</p>;
  }
  return (
    <div className="admin-skill-dependency-list">
      <p>
        Python {check.python_version} · {check.python_implementation} ·
        检查于 {formatDateTime(check.checked_at)}
      </p>
      <ul>
        {check.items.map((item) => (
          <li key={item.requirement}>
            <span
              className={`admin-status-badge ${
                item.satisfied ? " is-success" : " is-warning"
              }`}
            >
              {item.satisfied ? "已满足" : "缺失或不符"}
            </span>
            <code>{item.requirement}</code>
            <span>
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
    <div className="admin-skill-version-content">
      <div className="admin-skill-version-heading">
        <div>
          <strong>{versionLabel(version)}</strong>
          <p>
            {version.slug} · SHA-256{" "}
            <code>{version.package_sha256.slice(0, 16)}…</code>
          </p>
        </div>
        <DependencyBadge check={version.dependency_check} />
      </div>

      <section aria-labelledby={`guide-${version.id}`}>
        <h4 id={`guide-${version.id}`}>操作指南</h4>
        <div className="admin-skill-guide">
          {version.guide ? (
            <MarkdownContent text={version.guide} />
          ) : (
            <p className="admin-muted">该版本指南正文为空。</p>
          )}
        </div>
      </section>

      {/* 将文件清单、登记脚本与依赖检查编排为紧凑卡片网格，缩短详情区纵向滚动 */}
      <div className="admin-skill-version-meta-grid">
        <section aria-labelledby={`files-${version.id}`}>
          <h4 id={`files-${version.id}`}>文件清单</h4>
          <ul className="admin-skill-file-list">
            {version.files.map((file) => (
              <li key={file.path}>
                <span>
                  <strong>{file.path}</strong>
                  <small>
                    {file.text ? "UTF-8 文本" : "二进制资源"} ·{" "}
                    {formatBytes(file.size_bytes)}
                  </small>
                </span>
                <code>{file.sha256.slice(0, 12)}…</code>
              </li>
            ))}
          </ul>
        </section>

        <section aria-labelledby={`scripts-${version.id}`}>
          <h4 id={`scripts-${version.id}`}>登记脚本</h4>
          {version.scripts.length === 0 ? (
            <p className="admin-muted">该版本未登记可执行脚本。</p>
          ) : (
            <ul className="admin-skill-script-list">
              {version.scripts.map((script) => (
                <li key={script.path}>
                  <strong>{script.label}</strong>
                  <code>{script.path}</code>
                  <p>{script.purpose}</p>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section
          aria-labelledby={`dependencies-${version.id}`}
          className="admin-skill-version-dependencies"
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
      className="admin-skill-version-upload"
    >
      {/* 标题与说明独占首行，第二行按文件选择器、标签输入、上传按钮三列底部对齐 */}
      <div className="admin-skill-version-upload-header">
        <h4 id={`${inputId}-title`}>上传完整新版本</h4>
        <p>
          新包 slug 必须为 {slug}。上传只新增不可变版本，不自动切换。
        </p>
      </div>
      {/* 复用通用文件选择器并启用按钮触发模式，保持顶部标签对齐与跳过隐藏 input 的键盘焦点序 */}
      <div className="workspace-field admin-skill-file-picker-field">
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
      <label className="workspace-field" htmlFor={labelId}>
        <span>版本标签（可选）</span>
        <input
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
      <button
        className="button-quiet admin-skill-upload-submit"
        disabled={disabled || mutation.isPending || !file}
        onClick={() => void upload()}
        type="button"
      >
        <Icon name="upload" size={14} />
        {mutation.isPending ? "正在检查并上传…" : "上传新版本"}
      </button>
      {notice ? (
        <p className="admin-skill-inline-success" role="status">
          {notice}
        </p>
      ) : null}
      {error ? <ErrorBanner message={error} /> : null}
    </section>
  );
}

interface AdminSkillDetailDialogProps {
  onClose: () => void;
  open: boolean;
  skillId: string | null;
}

export function AdminSkillDetailDialog({
  onClose,
  open,
  skillId,
}: AdminSkillDetailDialogProps) {
  const { user } = useAuth();
  const versionSelectId = useId();
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
  const [requestError, setRequestError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const submitLock = useRef(false);

  useEffect(() => {
    setForm(null);
    setBaseline(null);
    setRevisionConflict(false);
    setConflictMessage(null);
    setRequestError(null);
    setNotice(null);
    setDeleteDialogOpen(false);
    setDeleteError(null);
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
    const nextForm = formFromSkill(skill);
    setBaseline({
      form: nextForm,
      revision: skill.revision,
      skillId: skill.id,
    });
    setForm(nextForm);
  }, [baseline?.skillId, detail.data, open]);

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
    const nextForm = formFromSkill(skill);
    setBaseline({
      form: nextForm,
      revision: skill.revision,
      skillId: skill.id,
    });
    setForm(nextForm);
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

    submitLock.current = true;
    setRequestError(null);
    setNotice(null);
    try {
      const saved = await update.mutateAsync({
        current_version_id: form.currentVersionId,
        enabled: form.enabled,
        expected_revision: baseline.revision,
        script_requires_approval: form.scriptRequiresApproval,
      });
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
  // 严格遵守后端契约：仅部门级技能允许软删除，全公司（company）技能绝不渲染删除入口。
  const isDepartmentSkill = currentScope === "department";
  const currentDepartmentName = detail.data
    ? (resolveUserDepartmentName(user, detail.data.owner_department_id) ??
      detail.data.owner_department_id ??
      null)
    : null;

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

  return (
    <Modal
      closeDisabled={pending}
      description="版本内容不可修改；当前版本、启停和脚本审批在同一修订号下保存。"
      footer={
        <>
          {isDepartmentSkill ? (
            <button
              className="button-danger-outline"
              disabled={pending || hasRevisionConflict}
              onClick={() => {
                setDeleteError(null);
                setDeleteDialogOpen(true);
              }}
              type="button"
            >
              <Icon name="trash" size={14} />
              删除部门技能
            </button>
          ) : null}
          <button
            className="button-quiet"
            disabled={pending}
            onClick={close}
            type="button"
          >
            关闭
          </button>
          <button
            className="button-primary"
            disabled={
              pending ||
              !form ||
              !selectedVersion ||
              hasRevisionConflict
            }
            onClick={() => void save()}
            type="button"
          >
            {update.isPending ? "正在保存配置…" : "保存当前配置"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      panelClassName="admin-modal admin-skill-detail-modal"
      title={
        detail.data ? `技能：${detail.data.name}` : "技能详情"
      }
    >
      {detail.isPending && !detail.data ? (
        <div className="page-loading admin-dialog-loading" role="status">
          正在读取技能版本与检查结果…
        </div>
      ) : detail.isError && !detail.data ? (
        <ErrorBanner
          message={skillErrorMessage(detail.error)}
          onRetry={() => void detail.refetch()}
        />
      ) : detail.data && form ? (
        <div className="admin-skill-detail">
          <section
            aria-labelledby="skill-config-title"
            className="admin-skill-config"
          >
            <header>
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h3 id="skill-config-title">当前发布配置</h3>
                  <SkillScopeBadge
                    departmentName={
                      isDepartmentSkill ? currentDepartmentName : null
                    }
                    scope={detail.data.scope}
                  />
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
              <div className="admin-inline-warning" role="alert">
                <p>
                  {conflictMessage ??
                    `服务端配置已更新到 revision ${detail.data.revision}，当前${isDirty ? "草稿" : "编辑基线"}仍保留在 revision ${baseline?.revision ?? "未知"}，保存已锁定。`}
                </p>
                <button
                  className="button-quiet"
                  disabled={pending}
                  onClick={() => void reloadLatestConfiguration()}
                  type="button"
                >
                  重新载入服务端最新配置
                </button>
              </div>
            ) : null}

            {/* 桌面端采用左右双栏网格：左侧版本选择，右侧启停与审批开关 */}
            <div className="admin-skill-config-grid">
              <div className="workspace-field">
                <div className="admin-skill-field-label">
                  <span>当前版本</span>
                  {isVersionDirty ? (
                    <span className="admin-skill-dirty-badge">未保存变更</span>
                  ) : null}
                </div>
                <SelectDropdown
                  classPrefix="admin-skill-dropdown"
                  disabled={pending}
                  id={versionSelectId}
                  label="当前版本"
                  listboxLabel="当前版本列表"
                  onChange={(nextVersionId) => {
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
                  options={[
                    ...(!selectedVersion && form.currentVersionId
                      ? [
                          {
                            badge: selectedDependencySatisfied
                              ? undefined
                              : "依赖未满足",
                            label: `${
                              detail.data.current_version_label ??
                              form.currentVersionId
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
                  ]}
                  placeholder="请选择版本"
                  value={form.currentVersionId}
                />
                <small>
                  保存后只影响新 Run；已创建 Run 始终使用创建时固定版本。
                </small>
              </div>

              <div className="admin-check-grid">
                <label className="workspace-checkbox">
                  <input
                    checked={form.enabled}
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
                <label className="workspace-checkbox">
                  <input
                    checked={form.scriptRequiresApproval}
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
            {!canEnable ? (
              <p className="admin-inline-warning">
                所选版本依赖未满足，后端将拒绝启用；可切换至检查通过的版本，或由部署管理员补齐统一 Python 环境。
              </p>
            ) : null}
            {currentVersionOutsideKnownList &&
            currentVersionDetail.isError ? (
              <ErrorBanner
                message="当前版本详情加载失败，暂时不能保存或预览该版本。"
                onRetry={() => void currentVersionDetail.refetch()}
              />
            ) : null}
            {selectedVersionNeedsDetail &&
            selectedVersionDetail.isError ? (
              <ErrorBanner
                message="所选版本详情加载失败，暂时不能保存或预览该版本。"
                onRetry={() => void selectedVersionDetail.refetch()}
              />
            ) : null}
            {requestError ? <ErrorBanner message={requestError} /> : null}
            {notice ? (
              <p className="admin-skill-inline-success" role="status">
                {notice}
              </p>
            ) : null}
          </section>

          <VersionUpload
            disabled={pending}
            onUploaded={() => setVersionPageNumber(1)}
            skillId={detail.data.id}
            slug={detail.data.slug}
          />

          <section
            aria-labelledby="skill-versions-title"
            className="admin-skill-versions"
          >
            <header>
              <div>
                <h3 id="skill-versions-title">不可变版本历史</h3>
                <p>选择上方表单中的版本可预览对应指南、资源与检查结果。</p>
              </div>
            </header>
            {/* 桌面端采用 Master-Detail 左右分栏：左侧分页与版本列表，右侧版本详情 */}
            <div className="admin-skill-versions-layout">
              <div className="admin-skill-versions-sidebar">
                <div
                  aria-label="版本历史分页"
                  className="admin-skill-version-pagination"
                >
                  <span>
                    共 {versionPage.data?.total ?? versions.length} 个版本
                  </span>
                  <button
                    className="button-quiet"
                    disabled={
                      versionPage.isFetching || versionPageNumber <= 1
                    }
                    onClick={() =>
                      setVersionPageNumber((current) =>
                        Math.max(1, current - 1),
                      )
                    }
                    type="button"
                  >
                    上一页版本
                  </button>
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
                  <button
                    className="button-quiet"
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
                    type="button"
                  >
                    下一页版本
                  </button>
                </div>
                {versionPage.isPending && versions.length === 0 ? (
                  <p className="admin-muted" role="status">
                    正在读取不可变版本历史…
                  </p>
                ) : versionPage.isError ? (
                  <ErrorBanner
                    message="版本历史分页加载失败；已保留服务端详情中的版本。"
                    onRetry={() => void versionPage.refetch()}
                  />
                ) : versions.length === 0 ? (
                  <p className="admin-muted">该技能尚无不可变版本。</p>
                ) : null}
                <ol className="admin-skill-version-list">
                  {versions.map((version) => (
                    <li
                      className={
                        version.id === form.currentVersionId
                          ? "is-selected"
                          : ""
                      }
                      key={version.id}
                    >
                      <button
                        className="admin-skill-version-tab"
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
                        <span>
                          <strong>{versionLabel(version)}</strong>
                          <small>
                            {formatDateTime(version.created_at)} ·{" "}
                            {version.files.length} 个文件
                          </small>
                        </span>
                        <DependencyBadge
                          check={version.dependency_check}
                        />
                      </button>
                    </li>
                  ))}
                </ol>
              </div>
              <div className="admin-skill-versions-detail">
                {selectedVersion ? (
                  <VersionContent version={selectedVersion} />
                ) : versionDetailPending ? (
                  <p className="admin-muted" role="status">
                    正在读取所选版本的指南、资源和依赖检查…
                  </p>
                ) : (
                  <p className="admin-muted">请选择一个版本查看内容。</p>
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
              <button
                className="button-quiet"
                disabled={deleteSkillMutation.isPending}
                onClick={() => setDeleteDialogOpen(false)}
                type="button"
              >
                取消
              </button>
              <button
                className="button-danger"
                disabled={deleteSkillMutation.isPending}
                onClick={() => void handleConfirmDeleteDepartmentSkill()}
                type="button"
              >
                {deleteSkillMutation.isPending ? "正在删除…" : "确认删除"}
              </button>
            </>
          }
          onClose={() => {
            if (!deleteSkillMutation.isPending) {
              setDeleteDialogOpen(false);
            }
          }}
          open={deleteDialogOpen}
          panelClassName="admin-modal"
          title="删除部门技能"
        >
          <p className="confirm-copy">
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
