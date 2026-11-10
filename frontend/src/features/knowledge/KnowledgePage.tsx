import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import {
  DeleteKnowledgeBaseDialog,
  EditKnowledgeBaseDialog,
  NewKnowledgeBaseDialog,
} from "./KnowledgeBaseDialogs";
import { knowledgeErrorMessage } from "./errorMessages";
import { useKnowledgeList } from "./hooks";
import type { KnowledgeBase } from "./model";

const PAGE_SIZE = 12;

function parsePage(value: string | null): number {
  if (!value) {
    return 1;
  }
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 1;
}

export function KnowledgePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const page = parsePage(searchParams.get("page"));
  const bases = useKnowledgeList({ page, pageSize: PAGE_SIZE });
  const [newBaseOpen, setNewBaseOpen] = useState(false);
  const [editBase, setEditBase] = useState<KnowledgeBase | null>(null);
  const [deleteBase, setDeleteBase] = useState<KnowledgeBase | null>(null);
  const responsePageSize = bases.data?.page_size ?? PAGE_SIZE;

  useEffect(() => {
    if (page > 1 && bases.data?.items.length === 0) {
      setSearchParams(
        page - 1 > 1 ? { page: String(page - 1) } : {},
        { replace: true },
      );
    }
  }, [bases.data, page, setSearchParams]);

  const goToPage = (nextPage: number) => {
    setSearchParams(
      nextPage > 1 ? { page: String(nextPage) } : {},
      { replace: true },
    );
  };

  const handleDeleted = () => {
    if (page > 1 && bases.data?.items.length === 1) {
      goToPage(page - 1);
    }
    setDeleteBase(null);
  };

  return (
    <div className="page-shell knowledge-page">
      <header className="page-heading">
        <div className="page-heading-copy">
          <p className="page-eyebrow">Workspace / Knowledge</p>
          <h1>知识库</h1>
          <p className="page-description">
            管理可检索的文档与文本资料。列表只展示后端真实返回的名称、说明和时间。
          </p>
        </div>
        <button
          className="button-primary"
          onClick={() => setNewBaseOpen(true)}
          type="button"
        >
          <Icon name="plus" size={14} />
          新建知识库
        </button>
      </header>

      {bases.isPending ? (
        <div className="page-loading" role="status">
          正在加载知识库…
        </div>
      ) : bases.isError ? (
        <ErrorBanner
          message={knowledgeErrorMessage(bases.error)}
          onRetry={() => void bases.refetch()}
        />
      ) : bases.data.items.length === 0 && page === 1 ? (
        <EmptyState
          action={
            <button
              className="button-primary"
              onClick={() => setNewBaseOpen(true)}
              type="button"
            >
              <Icon name="plus" size={14} />
              新建知识库
            </button>
          }
          description="创建知识库后，可以上传文件或粘贴文本，再在项目中关联使用。"
          icon="book"
          kicker="No knowledge bases yet"
          title="还没有知识库"
        />
      ) : (
        <section aria-labelledby="knowledge-list-title">
          <div className="surface-header knowledge-list-header">
            <div className="surface-title">
              <strong id="knowledge-list-title">全部知识库</strong>
              <span>按后端更新时间倒序排列</span>
            </div>
            <span className="knowledge-page-count">第 {page} 页</span>
          </div>

          <ul className="knowledge-card-grid">
            {bases.data.items.map((base) => (
              <li className="knowledge-card" key={base.id}>
                <div className="knowledge-card-main">
                  <p className="knowledge-card-kicker">Knowledge base</p>
                  <Link
                    className="knowledge-card-title"
                    to={`/knowledge/${encodeURIComponent(base.id)}`}
                  >
                    <strong>{base.name}</strong>
                    <Icon name="arrow-right" size={16} />
                  </Link>
                  <p className="knowledge-card-description">
                    {base.description || "未填写说明"}
                  </p>
                </div>
                <dl className="knowledge-card-meta">
                  <div>
                    <dt>创建</dt>
                    <dd>{formatDateTime(base.created_at)}</dd>
                  </div>
                  <div>
                    <dt>更新</dt>
                    <dd>{formatDateTime(base.updated_at)}</dd>
                  </div>
                </dl>
                <div className="knowledge-card-actions">
                  <button
                    aria-label={`编辑知识库：${base.name}`}
                    className="button-quiet knowledge-card-action"
                    onClick={() => setEditBase(base)}
                    type="button"
                  >
                    <Icon name="edit" size={14} />
                    编辑
                  </button>
                  <button
                    aria-label={`删除知识库：${base.name}`}
                    className="button-danger-outline knowledge-card-action"
                    onClick={() => setDeleteBase(base)}
                    type="button"
                  >
                    <Icon name="trash" size={14} />
                    删除
                  </button>
                </div>
              </li>
            ))}
          </ul>

          <footer className="project-list-footer knowledge-list-footer">
            <span>
              本页 {bases.data.items.length} 个知识库
              {bases.data.items.length >= responsePageSize
                ? "（可能还有下一页）"
                : ""}
            </span>
            <nav className="project-pagination" aria-label="知识库分页">
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
                disabled={bases.data.items.length < responsePageSize}
                onClick={() => goToPage(page + 1)}
                type="button"
              >
                <Icon name="chevron" size={13} />
              </button>
            </nav>
          </footer>
        </section>
      )}

      <NewKnowledgeBaseDialog
        onClose={() => setNewBaseOpen(false)}
        open={newBaseOpen}
      />
      {editBase ? (
        <EditKnowledgeBaseDialog
          base={editBase}
          onClose={() => setEditBase(null)}
          open
        />
      ) : null}
      {deleteBase ? (
        <DeleteKnowledgeBaseDialog
          base={deleteBase}
          onClose={() => setDeleteBase(null)}
          onDeleted={handleDeleted}
          open
        />
      ) : null}
    </div>
  );
}
