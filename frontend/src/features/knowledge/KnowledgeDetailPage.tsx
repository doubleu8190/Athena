import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import { parsePage } from "../../shared/lib/pagination";
import {
  Badge,
  Button,
  ICON_BUTTON_CLASS,
  ICON_BUTTON_DANGER_CLASS,
  LIST_FOOTER_CLASS,
  PAGE_LOADING_CLASS,
  PAGE_NAV_BUTTON_CLASS,
  PAGE_SHELL_CLASS,
  SURFACE_CARD_CLASS,
  TEXT_BUTTON_CLASS,
  VisibilityBadge,
  cn,
} from "../../shared/ui";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { useAuth } from "../auth/AuthProvider";
import { resolveUserDepartmentName } from "../auth/model";
import {
  DeleteKnowledgeBaseDialog,
  EditKnowledgeBaseDialog,
} from "./KnowledgeBaseDialogs";
import {
  DeleteKnowledgeDocumentDialog,
  KnowledgeIngestDialog,
  RenameKnowledgeDocumentDialog,
  VersionHistoryDialog,
} from "./KnowledgeDocumentDialogs";
import {
  isKnowledgeNotFound,
  knowledgeErrorMessage,
  versionErrorMessage,
} from "./errorMessages";
import { formatKnowledgeWarnings } from "./display";
import {
  useKnowledgeBase,
  useKnowledgeDocuments,
  useRetryKnowledgeVersionMutation,
} from "./hooks";
import { KnowledgeSearchDialog } from "./KnowledgeSearchDialog";
import { KnowledgeStateBadge } from "./KnowledgeStateBadge";
import {
  canManageKnowledgeBase,
  countExtraVisibilityGrants,
  type KnowledgeDocument,
} from "./model";

const PAGE_SIZE = 10;

interface DocumentRowProps {
  baseId: string;
  canManage: boolean;
  document: KnowledgeDocument;
  onDelete: (document: KnowledgeDocument) => void;
  onRename: (document: KnowledgeDocument) => void;
  onReplace: (document: KnowledgeDocument) => void;
  onVersions: (document: KnowledgeDocument) => void;
}

// 文档列表响应式列宽定义，桌面端 4 列展示，窄屏（<=700px）自动折叠为信息行与右上操作区。
const DOC_GRID_COLS_CLASS =
  "min-[701px]:grid-cols-[minmax(190px,1.2fr)_minmax(220px,1.35fr)_110px_138px] min-[701px]:gap-2.5 min-[1181px]:grid-cols-[minmax(220px,1.25fr)_minmax(250px,1.45fr)_128px_150px] min-[1181px]:gap-4";

function DocumentRow({
  baseId,
  canManage,
  document,
  onDelete,
  onRename,
  onReplace,
  onVersions,
}: DocumentRowProps) {
  // 直接消费文档列表内联返回的 latest_version 摘要，由列表查询统一轮询 queued/processing 状态，不按行请求 /versions。
  const retry = useRetryKnowledgeVersionMutation(baseId, document.id);
  const latest = document.latest_version;
  const hasNoVersions = latest === null;
  const canRetry =
    canManage &&
    latest?.state === "failed" &&
    document.pending_version_id === null;
  const activeVersionAvailable = document.active_version_id !== null;

  return (
    <article
      className={cn(
        "grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-[13px] gap-y-2.5 border-b border-border-subtle px-[15px] py-3.5 transition-colors hover:bg-subtle min-[701px]:min-h-[76px] min-[701px]:px-[18px] min-[701px]:py-[11px]",
        DOC_GRID_COLS_CLASS,
      )}
    >
      <div className="col-span-full flex min-w-0 items-center gap-2.5 min-[701px]:col-span-1">
        <span
          aria-hidden="true"
          className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-brand-border bg-brand-subtle text-brand-hover"
        >
          <Icon name="book" size={16} />
        </span>
        <div className="flex min-w-0 flex-col gap-[3px]">
          <strong className="truncate text-sm font-semibold text-text-primary">
            {document.title}
          </strong>
          <span className="block text-xs leading-[1.55] text-text-tertiary min-[701px]:hidden">
            更新于 {formatDateTime(document.updated_at)}
          </span>
        </div>
      </div>

      <div
        className="col-span-full flex min-w-0 flex-col items-start gap-1 text-xs leading-[1.55] text-text-tertiary min-[701px]:col-span-1"
        aria-live="polite"
      >
        <KnowledgeStateBadge state={latest?.state ?? null} />
        {latest?.state === "ready" ? (
          <span>
            {latest.chunk_count} 个检索片段
            {latest.warnings.length > 0
              ? ` · ${latest.warnings.length} 条警告：${formatKnowledgeWarnings(
                  latest.warnings,
                )}`
              : ""}
          </span>
        ) : null}
        {latest?.state === "failed" ? (
          <span className="text-xs leading-[1.5] text-danger">
            {versionErrorMessage(latest.error_code)}
            {activeVersionAvailable
              ? " 当前检索仍使用上一已就绪版本。"
              : " 当前没有可检索版本。"}
          </span>
        ) : null}
        {latest?.state === "queued" || latest?.state === "processing" ? (
          <span>
            {activeVersionAvailable
              ? "当前仍使用上一已就绪版本。"
              : "处理完成后才可检索。"}
          </span>
        ) : null}
        {hasNoVersions ? <span>后端未返回版本记录。</span> : null}
        {canRetry && latest ? (
          <button
            className={TEXT_BUTTON_CLASS}
            disabled={retry.isPending}
            onClick={() => retry.mutate(latest.id)}
            type="button"
          >
            {retry.isPending ? "正在重试…" : "重试失败版本"}
          </button>
        ) : null}
        {retry.isError ? (
          <span
            className="inline-flex flex-wrap gap-1 text-xs leading-[1.5] text-danger"
            role="alert"
          >
            {knowledgeErrorMessage(retry.error)}
          </span>
        ) : null}
        {retry.isSuccess ? (
          <span className="text-xs text-success" role="status">
            已重新排队
          </span>
        ) : null}
      </div>

      <time
        className="hidden font-mono text-xs leading-[1.55] text-text-tertiary min-[701px]:block"
        dateTime={document.updated_at}
      >
        {formatDateTime(document.updated_at)}
      </time>

      <div className="col-start-2 grid grid-cols-[repeat(2,31px)] justify-end gap-0.5 min-[421px]:flex min-[701px]:col-start-auto">
        {canManage ? (
          <>
            <button
              aria-label={`重命名文档：${document.title}`}
              className={ICON_BUTTON_CLASS}
              onClick={() => onRename(document)}
              title="重命名文档"
              type="button"
            >
              <Icon name="edit" size={15} />
            </button>
            <button
              aria-label={`添加新版本：${document.title}`}
              className={ICON_BUTTON_CLASS}
              disabled={document.pending_version_id !== null}
              onClick={() => onReplace(document)}
              title={
                document.pending_version_id
                  ? "已有版本正在处理中"
                  : "添加新版本"
              }
              type="button"
            >
              <Icon name="upload" size={15} />
            </button>
          </>
        ) : null}
        <button
          aria-label={`版本历史：${document.title}`}
          className={ICON_BUTTON_CLASS}
          onClick={() => onVersions(document)}
          title="版本历史"
          type="button"
        >
          <Icon name="history" size={15} />
        </button>
        {canManage ? (
          <button
            aria-label={`删除文档：${document.title}`}
            className={cn(ICON_BUTTON_CLASS, ICON_BUTTON_DANGER_CLASS)}
            onClick={() => onDelete(document)}
            title="删除文档"
            type="button"
          >
            <Icon name="trash" size={15} />
          </button>
        ) : null}
      </div>
    </article>
  );
}

export function KnowledgeDetailPage() {
  const { user } = useAuth();
  const { baseId } = useParams<{ baseId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const page = parsePage(searchParams.get("page"));
  const base = useKnowledgeBase(baseId);
  const documents = useKnowledgeDocuments(
    baseId ?? "",
    { page, pageSize: PAGE_SIZE },
    Boolean(baseId) && base.isSuccess,
  );
  const [editBaseOpen, setEditBaseOpen] = useState(false);
  const [deleteBaseOpen, setDeleteBaseOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [renameDocument, setRenameDocument] =
    useState<KnowledgeDocument | null>(null);
  const [replaceDocument, setReplaceDocument] =
    useState<KnowledgeDocument | null>(null);
  const [versionsDocument, setVersionsDocument] =
    useState<KnowledgeDocument | null>(null);
  const [deleteDocument, setDeleteDocument] =
    useState<KnowledgeDocument | null>(null);
  const responsePageSize = documents.data?.page_size ?? PAGE_SIZE;

  useEffect(() => {
    if (
      base.isSuccess &&
      page > 1 &&
      documents.data?.items.length === 0
    ) {
      setSearchParams(
        page - 1 > 1 ? { page: String(page - 1) } : {},
        { replace: true },
      );
    }
  }, [base.isSuccess, documents.data, page, setSearchParams]);

  const goToPage = (nextPage: number) => {
    setSearchParams(
      nextPage > 1 ? { page: String(nextPage) } : {},
      { replace: true },
    );
  };

  const handleImported = () => {
    if (page !== 1) {
      goToPage(1);
    }
  };

  const handleDocumentDeleted = () => {
    if (page > 1 && documents.data?.items.length === 1) {
      goToPage(page - 1);
    }
    setDeleteDocument(null);
  };

  if (!baseId) {
    return <NavigateToKnowledge />;
  }

  if (base.isPending) {
    return (
      <div className={PAGE_SHELL_CLASS}>
        <div className={PAGE_LOADING_CLASS} role="status">
          正在加载知识库…
        </div>
      </div>
    );
  }

  if (base.isError) {
    const notFound = isKnowledgeNotFound(base.error);
    return (
      <div className={cn(PAGE_SHELL_CLASS, "flex flex-col gap-3.5")}>
        <EmptyState
          action={
            <Button asChild variant="primary">
              <Link to="/knowledge">返回知识库</Link>
            </Button>
          }
          description={
            notFound
              ? "该知识库不存在、已删除，或你当前无权访问。"
              : "知识库信息暂时无法加载，请稍后重试。"
          }
          icon="book"
          title={notFound ? "知识库不存在或不可访问" : "知识库加载失败"}
        />
        {!notFound ? (
          <ErrorBanner
            message={knowledgeErrorMessage(base.error)}
            onRetry={() => void base.refetch()}
          />
        ) : null}
      </div>
    );
  }

  const scope = base.data.visibility_scope;
  const departmentName = resolveUserDepartmentName(
    user,
    base.data.owner_department_id,
  );
  const extraGrantCount = countExtraVisibilityGrants(base.data);
  const canManage = canManageKnowledgeBase(user, base.data);

  return (
    <div className={cn(PAGE_SHELL_CLASS, "lg:pt-8")}>
      <header className="mb-6 rounded-[14px] border border-border-default bg-canvas p-[18px] shadow-xs sm:p-5 lg:px-[26px] lg:py-6">
        <div className="flex flex-col items-start justify-between gap-4 lg:flex-row lg:gap-6">
          <div className="min-w-0">
            <div className="mb-1.5 flex flex-wrap items-center gap-2">
              <VisibilityBadge
                departmentName={departmentName}
                scope={scope}
              />
              {extraGrantCount > 0 ? (
                <Badge variant="info">额外共享 {extraGrantCount} 项</Badge>
              ) : null}
              {!canManage ? <Badge variant="neutral">只读</Badge> : null}
            </div>
            <h1 className="text-[clamp(24px,2.8vw,32px)] leading-[1.2] font-semibold tracking-[-0.025em] text-text-primary [overflow-wrap:anywhere]">
              {base.data.name}
            </h1>
            <p className="mt-2 max-w-[690px] text-sm leading-[1.65] text-text-secondary">
              {base.data.description || "未填写知识库说明"}
            </p>
            <p className="mt-2.5 font-mono text-xs leading-[1.6] text-text-tertiary">
              创建于 {formatDateTime(base.data.created_at)} · 更新于{" "}
              {formatDateTime(base.data.updated_at)}
            </p>
          </div>
          <div className="flex w-full shrink-0 flex-wrap justify-start gap-2 max-[700px]:[&>*]:flex-[1_1_145px] lg:w-auto lg:justify-end">
            <Button
              onClick={() => setSearchOpen(true)}
              type="button"
              variant="outline"
            >
              <Icon name="search" size={14} />
              检索测试
            </Button>
            {canManage ? (
              <>
                <Button
                  onClick={() => setEditBaseOpen(true)}
                  type="button"
                  variant="outline"
                >
                  <Icon name="edit" size={14} />
                  编辑
                </Button>
                <Button
                  onClick={() => setImportOpen(true)}
                  type="button"
                  variant="primary"
                >
                  <Icon name="plus" size={14} />
                  添加资料
                </Button>
              </>
            ) : null}
          </div>
        </div>
      </header>

      <div className="mb-3.5 flex flex-col items-start justify-between gap-4 px-0.5 py-1 min-[701px]:flex-row min-[701px]:items-end min-[701px]:gap-6 max-[700px]:[&>button]:w-full">
        <div>
          <h2 className="text-xl font-semibold tracking-[-0.02em] text-text-primary">
            文档
          </h2>
          <p className="mt-1 max-w-[620px] text-[13px] leading-[1.6] text-text-secondary">
            处理状态来自最新版本接口；排队和处理中会自动刷新，且不展示虚构进度。
          </p>
        </div>
        {canManage ? (
          <Button
            onClick={() => setDeleteBaseOpen(true)}
            type="button"
            variant="danger-outline"
          >
            <Icon name="trash" size={14} />
            删除知识库
          </Button>
        ) : null}
      </div>

      {documents.isPending ? (
        <div className={PAGE_LOADING_CLASS} role="status">
          正在加载文档…
        </div>
      ) : documents.isError ? (
        <ErrorBanner
          message={knowledgeErrorMessage(documents.error)}
          onRetry={() => void documents.refetch()}
        />
      ) : documents.data.items.length === 0 && page === 1 ? (
        <EmptyState
          action={
            canManage ? (
              <Button
                onClick={() => setImportOpen(true)}
                type="button"
                variant="primary"
              >
                <Icon name="plus" size={14} />
                添加资料
              </Button>
            ) : undefined
          }
          description={
            canManage
              ? "上传文件或粘贴文本后，后端会在后台完成解析、OCR 和索引。"
              : "当前知识库尚未导入文档，你可以稍后再来查看或检索。"
          }
          icon="upload"
          title="还没有文档"
        />
      ) : (
        <section
          aria-label="知识库文档列表"
          className={cn(SURFACE_CARD_CLASS, "overflow-hidden")}
        >
          <div
            aria-hidden="true"
            className={cn(
              "hidden min-h-[39px] items-center border-b border-border-subtle bg-subtle px-[18px] font-mono text-xs tracking-[0.06em] text-text-tertiary uppercase min-[701px]:grid [&>:last-child]:text-right",
              DOC_GRID_COLS_CLASS,
            )}
          >
            <span>文档</span>
            <span>状态</span>
            <span>更新时间</span>
            <span>操作</span>
          </div>
          {documents.data.items.map((document) => (
            <DocumentRow
              baseId={baseId}
              canManage={canManage}
              document={document}
              key={document.id}
              onDelete={setDeleteDocument}
              onRename={setRenameDocument}
              onReplace={setReplaceDocument}
              onVersions={setVersionsDocument}
            />
          ))}
          <footer className={LIST_FOOTER_CLASS}>
            <span>
              本页 {documents.data.items.length} 份文档
              {documents.data.items.length >= responsePageSize
                ? "（可能还有下一页）"
                : ""}
            </span>
            <nav className="flex items-center gap-2" aria-label="文档分页">
              <button
                aria-label="上一页"
                className={cn(PAGE_NAV_BUTTON_CLASS, "[&>svg]:rotate-180")}
                disabled={page <= 1}
                onClick={() => goToPage(page - 1)}
                type="button"
              >
                <Icon name="chevron" size={13} />
              </button>
              <span className="text-xs text-text-secondary">第 {page} 页</span>
              <button
                aria-label="下一页"
                className={PAGE_NAV_BUTTON_CLASS}
                disabled={documents.data.items.length < responsePageSize}
                onClick={() => goToPage(page + 1)}
                type="button"
              >
                <Icon name="chevron" size={13} />
              </button>
            </nav>
          </footer>
        </section>
      )}

      <KnowledgeSearchDialog
        baseId={baseId}
        onClose={() => setSearchOpen(false)}
        open={searchOpen}
      />

      {canManage ? (
        <>
          <KnowledgeIngestDialog
            baseId={baseId}
            onClose={() => setImportOpen(false)}
            onImported={handleImported}
            open={importOpen}
          />
          {editBaseOpen ? (
            <EditKnowledgeBaseDialog
              base={base.data}
              onClose={() => setEditBaseOpen(false)}
              open
            />
          ) : null}
          {deleteBaseOpen ? (
            <DeleteKnowledgeBaseDialog
              base={base.data}
              onClose={() => setDeleteBaseOpen(false)}
              open
            />
          ) : null}
          {renameDocument ? (
            <RenameKnowledgeDocumentDialog
              baseId={baseId}
              document={renameDocument}
              onClose={() => setRenameDocument(null)}
              open
            />
          ) : null}
          {replaceDocument ? (
            <KnowledgeIngestDialog
              baseId={baseId}
              document={replaceDocument}
              onClose={() => setReplaceDocument(null)}
              onImported={handleImported}
              open
            />
          ) : null}
          {deleteDocument ? (
            <DeleteKnowledgeDocumentDialog
              baseId={baseId}
              document={deleteDocument}
              onClose={() => setDeleteDocument(null)}
              onDeleted={handleDocumentDeleted}
              open
            />
          ) : null}
        </>
      ) : null}

      {versionsDocument ? (
        <VersionHistoryDialog
          baseId={baseId}
          canManage={canManage}
          document={versionsDocument}
          onClose={() => setVersionsDocument(null)}
          open
        />
      ) : null}
    </div>
  );
}

function NavigateToKnowledge() {
  return (
    <div className={PAGE_SHELL_CLASS}>
      <EmptyState
        action={
          <Button asChild variant="primary">
            <Link to="/knowledge">返回知识库</Link>
          </Button>
        }
        description="缺少知识库标识，无法打开详情页。"
        icon="book"
        title="知识库地址无效"
      />
    </div>
  );
}
