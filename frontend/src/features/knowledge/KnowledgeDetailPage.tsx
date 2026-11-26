import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import { Badge, VisibilityBadge } from "../../shared/ui";
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

function parsePage(value: string | null): number {
  if (!value) {
    return 1;
  }
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 1;
}

interface DocumentRowProps {
  baseId: string;
  canManage: boolean;
  document: KnowledgeDocument;
  onDelete: (document: KnowledgeDocument) => void;
  onRename: (document: KnowledgeDocument) => void;
  onReplace: (document: KnowledgeDocument) => void;
  onVersions: (document: KnowledgeDocument) => void;
}

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
    <article className="knowledge-document-row">
      <div className="knowledge-document-main">
        <span className="knowledge-document-icon" aria-hidden="true">
          <Icon name="book" size={16} />
        </span>
        <div>
          <strong>{document.title}</strong>
          <span>更新于 {formatDateTime(document.updated_at)}</span>
        </div>
      </div>

      <div className="knowledge-document-status" aria-live="polite">
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
          <span className="knowledge-row-error">
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
            className="text-button"
            disabled={retry.isPending}
            onClick={() => retry.mutate(latest.id)}
            type="button"
          >
            {retry.isPending ? "正在重试…" : "重试失败版本"}
          </button>
        ) : null}
        {retry.isError ? (
          <span className="knowledge-inline-error" role="alert">
            {knowledgeErrorMessage(retry.error)}
          </span>
        ) : null}
        {retry.isSuccess ? (
          <span className="knowledge-status-success" role="status">
            已重新排队
          </span>
        ) : null}
      </div>

      <time
        className="knowledge-document-time"
        dateTime={document.updated_at}
      >
        {formatDateTime(document.updated_at)}
      </time>

      <div className="knowledge-document-actions">
        {canManage ? (
          <>
            <button
              aria-label={`重命名文档：${document.title}`}
              className="icon-button"
              onClick={() => onRename(document)}
              title="重命名文档"
              type="button"
            >
              <Icon name="edit" size={15} />
            </button>
            <button
              aria-label={`添加新版本：${document.title}`}
              className="icon-button"
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
          className="icon-button"
          onClick={() => onVersions(document)}
          title="版本历史"
          type="button"
        >
          <Icon name="history" size={15} />
        </button>
        {canManage ? (
          <button
            aria-label={`删除文档：${document.title}`}
            className="icon-button icon-button--danger"
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
      <div className="page-shell">
        <div className="page-loading" role="status">
          正在加载知识库…
        </div>
      </div>
    );
  }

  if (base.isError) {
    const notFound = isKnowledgeNotFound(base.error);
    return (
      <div className="page-shell project-error-page">
        <EmptyState
          action={
            <Link className="button-primary" to="/knowledge">
              返回知识库
            </Link>
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
    <div className="page-shell knowledge-detail-page">
      <header className="knowledge-hero">
        <div className="knowledge-hero-top">
          <div className="knowledge-hero-copy">
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
            <h1>{base.data.name}</h1>
            <p className="knowledge-hero-description">
              {base.data.description || "未填写知识库说明"}
            </p>
            <p className="project-meta">
              创建于 {formatDateTime(base.data.created_at)} · 更新于{" "}
              {formatDateTime(base.data.updated_at)}
            </p>
          </div>
          <div className="knowledge-hero-actions">
            <button
              className="button-quiet"
              onClick={() => setSearchOpen(true)}
              type="button"
            >
              <Icon name="search" size={14} />
              检索测试
            </button>
            {canManage ? (
              <>
                <button
                  className="button-quiet"
                  onClick={() => setEditBaseOpen(true)}
                  type="button"
                >
                  <Icon name="edit" size={14} />
                  编辑
                </button>
                <button
                  className="button-primary"
                  onClick={() => setImportOpen(true)}
                  type="button"
                >
                  <Icon name="plus" size={14} />
                  添加资料
                </button>
              </>
            ) : null}
          </div>
        </div>
      </header>

      <div className="knowledge-detail-toolbar">
        <div>
          <h2>文档</h2>
          <p>
            处理状态来自最新版本接口；排队和处理中会自动刷新，且不展示虚构进度。
          </p>
        </div>
        {canManage ? (
          <button
            className="button-danger-outline"
            onClick={() => setDeleteBaseOpen(true)}
            type="button"
          >
            <Icon name="trash" size={14} />
            删除知识库
          </button>
        ) : null}
      </div>

      {documents.isPending ? (
        <div className="page-loading" role="status">
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
              <button
                className="button-primary"
                onClick={() => setImportOpen(true)}
                type="button"
              >
                <Icon name="plus" size={14} />
                添加资料
              </button>
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
          className="surface-card knowledge-document-table"
        >
          <div className="knowledge-document-head" aria-hidden="true">
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
          <footer className="project-list-footer knowledge-list-footer">
            <span>
              本页 {documents.data.items.length} 份文档
              {documents.data.items.length >= responsePageSize
                ? "（可能还有下一页）"
                : ""}
            </span>
            <nav className="project-pagination" aria-label="文档分页">
              <button
                aria-label="上一页"
                className="project-page-button"
                disabled={page <= 1}
                onClick={() => goToPage(page - 1)}
                type="button"
              >
                <Icon name="chevron" size={13} />
              </button>
              <span className="project-page-status">第 {page} 页</span>
              <button
                aria-label="下一页"
                className="project-page-button project-page-button--next"
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
    <div className="page-shell">
      <EmptyState
        action={
          <Link className="button-primary" to="/knowledge">
            返回知识库
          </Link>
        }
        description="缺少知识库标识，无法打开详情页。"
        icon="book"
        title="知识库地址无效"
      />
    </div>
  );
}
