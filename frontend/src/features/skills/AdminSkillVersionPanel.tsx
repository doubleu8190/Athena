import { Upload } from "lucide-react";
import { useId, useRef, useState } from "react";

import { formatBytes, formatDateTime } from "../../shared/lib/format";
import { newRequestId } from "../../shared/lib/requestId";
import { useSubmitLock } from "../../shared/lib/useSubmitLock";
import {
  ADMIN_MUTED_CLASS,
  Badge,
  Button,
  FIELD_CLASS,
  FIELD_INPUT_CLASS,
  InlineAlert,
  NOTICE_SUCCESS_CLASS,
  TEXT_BUTTON_CLASS,
  cn,
} from "../../shared/ui";
import { MarkdownContent } from "../../shared/ui/MarkdownContent";
import { WorkspaceFilePicker } from "../../shared/ui/WorkspaceFilePicker";
import { skillErrorMessage } from "./errorMessages";
import { useUploadSkillVersionMutation } from "./hooks";
import {
  dependencySatisfied,
  validateSkillZip,
  versionLabel,
  type DependencyCheck,
  type SkillVersion,
} from "./model";

export const VERSION_PAGE_SIZE = 20;

export const DETAIL_PANEL_CLASS =
  "min-w-0 rounded-[11px] border border-border-default bg-canvas p-4";
export const DETAIL_PANEL_HEADER_CLASS =
  "mb-3 flex flex-col items-start justify-between gap-4 border-b border-border-subtle pb-2.5 min-[721px]:flex-row [&_h3]:text-base [&_h3]:leading-[1.3] [&_h3]:font-semibold [&_h3]:text-text-primary [&_p]:mt-[3px] [&_p]:text-xs [&_p]:leading-[1.6] [&_p]:text-text-tertiary [&_code]:font-mono [&_code]:text-xs [&_code]:[overflow-wrap:anywhere]";

const VERSION_ITEM_LIST_CLASS = "m-0 flex list-none flex-col gap-[7px] p-0";
const VERSION_ITEM_ROW_CLASS =
  "flex min-w-0 items-start justify-between gap-3 rounded-lg border border-border-default bg-canvas px-2.5 py-[9px]";

/** 技能运行环境依赖满足状态徽章 */
export function DependencyBadge({ check }: { check: DependencyCheck | null }) {
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

/** ZIP 脚本包技能专用：上传并校验不可变新版本 */
export function VersionUpload({
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
  const uploadLock = useSubmitLock();
  const mutation = useUploadSkillVersionMutation(skillId);
  const uploading = mutation.isPending || uploadLock.isLocked;

  const upload = async () => {
    const validation = validateSkillZip(file);
    if (validation) {
      setError(validation);
      return;
    }
    if (!file || uploading) {
      return;
    }
    // 同一文件与标签失败重试复用原幂等键，改选文件或修改标签时轮换新键。
    await uploadLock.runLocked(async () => {
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
    });
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
          disabled={disabled || uploading}
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
          disabled={disabled || uploading}
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
        disabled={disabled || uploading || !file}
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
        {uploading ? "正在检查并上传…" : "上传新版本"}
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

interface AdminSkillVersionPanelProps {
  currentVersionId: string;
  disabled: boolean;
  onPageChange: (nextPage: number) => void;
  onRetryPage: () => void;
  onSelectVersion: (versionId: string) => void;
  pageError: boolean;
  pageFetching: boolean;
  pageNumber: number;
  pagePending: boolean;
  pageResultPage?: number;
  pageTotal?: number;
  selectedVersion: SkillVersion | undefined;
  versionDetailPending: boolean;
  versions: readonly SkillVersion[];
}

/** 技能不可变版本历史 Master-Detail 展示面板 */
export function AdminSkillVersionPanel({
  currentVersionId,
  disabled,
  onPageChange,
  onRetryPage,
  onSelectVersion,
  pageError,
  pageFetching,
  pageNumber,
  pagePending,
  pageResultPage,
  pageTotal,
  selectedVersion,
  versionDetailPending,
  versions,
}: AdminSkillVersionPanelProps) {
  const totalCount = pageTotal ?? versions.length;
  const totalPages = Math.max(
    1,
    Math.ceil((pageTotal ?? 0) / VERSION_PAGE_SIZE),
  );
  const hasPageData = pageTotal !== undefined;

  return (
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
              共 {totalCount} 个版本
            </span>
            <Button
              disabled={pageFetching || pageNumber <= 1}
              onClick={() => onPageChange(Math.max(1, pageNumber - 1))}
              size="xs"
              type="button"
              variant="outline"
            >
              上一页版本
            </Button>
            <span>
              第 {pageResultPage ?? pageNumber} / {totalPages} 页
            </span>
            <Button
              disabled={pageFetching || !hasPageData || pageNumber >= totalPages}
              onClick={() => onPageChange(pageNumber + 1)}
              size="xs"
              type="button"
              variant="outline"
            >
              下一页版本
            </Button>
          </div>
          {pagePending && versions.length === 0 ? (
            <p className={ADMIN_MUTED_CLASS} role="status">
              正在读取不可变版本历史…
            </p>
          ) : pageError ? (
            <InlineAlert
              action={
                <button
                  className={TEXT_BUTTON_CLASS}
                  onClick={onRetryPage}
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
              const isSelected = version.id === currentVersionId;
              return (
                <li className="shrink-0" key={version.id}>
                  <button
                    className={cn(
                      "flex w-full min-w-0 items-start justify-between gap-2 rounded-[9px] border px-[11px] py-2.5 text-left transition-colors hover:border-brand-border hover:bg-brand-subtle",
                      isSelected
                        ? "border-brand-border bg-brand-subtle shadow-[inset_3px_0_0_var(--brand-hover)]"
                        : "border-border-default bg-canvas",
                    )}
                    disabled={disabled}
                    onClick={() => onSelectVersion(version.id)}
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
  );
}
