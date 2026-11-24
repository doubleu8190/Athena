import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import {
  Badge,
  Tabs,
  TabsList,
  TabsTrigger,
  VisibilityBadge,
  type VisibilityScope,
} from "../../shared/ui";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { useAuth } from "../auth/AuthProvider";
import { resolveUserDepartmentName } from "../auth/model";
import {
  DeleteKnowledgeBaseDialog,
  EditKnowledgeBaseDialog,
  NewKnowledgeBaseDialog,
} from "./KnowledgeBaseDialogs";
import { knowledgeErrorMessage } from "./errorMessages";
import { useKnowledgeList } from "./hooks";
import {
  canManageKnowledgeBase,
  countExtraVisibilityGrants,
  knowledgeBaseScope,
  type KnowledgeBase,
} from "./model";

const PAGE_SIZE = 12;

type ScopeFilterValue = "all" | VisibilityScope;

const SCOPE_FILTER_OPTIONS: ReadonlyArray<{
  label: string;
  value: ScopeFilterValue;
}> = [
  { label: "全部", value: "all" },
  { label: "全公司", value: "company" },
  { label: "部门共享", value: "department" },
  { label: "个人", value: "personal" },
];

function parsePage(value: string | null): number {
  if (!value) {
    return 1;
  }
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 1;
}

/** 同时兼容 ?scope=（M2.6 筛选参数）与侧栏部门快捷入口 ?visibility_scope=department。 */
function parseScopeFilter(searchParams: URLSearchParams): ScopeFilterValue {
  const raw =
    searchParams.get("scope") ?? searchParams.get("visibility_scope");
  if (raw === "company" || raw === "department" || raw === "personal") {
    return raw;
  }
  return "all";
}

function buildSearchParams(
  scope: ScopeFilterValue,
  nextPage: number,
): Record<string, string> {
  const params: Record<string, string> = {};
  if (scope !== "all") {
    params.scope = scope;
  }
  if (nextPage > 1) {
    params.page = String(nextPage);
  }
  return params;
}

export function KnowledgePage() {
  const { user } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const page = parsePage(searchParams.get("page"));
  const scopeFilter = parseScopeFilter(searchParams);
  const bases = useKnowledgeList({
    page,
    pageSize: PAGE_SIZE,
    ...(scopeFilter !== "all" ? { visibilityScope: scopeFilter } : {}),
  });
  const [newBaseOpen, setNewBaseOpen] = useState(false);
  const [editBase, setEditBase] = useState<KnowledgeBase | null>(null);
  const [deleteBase, setDeleteBase] = useState<KnowledgeBase | null>(null);
  const responsePageSize = bases.data?.page_size ?? PAGE_SIZE;

  useEffect(() => {
    if (page > 1 && bases.data?.items.length === 0) {
      setSearchParams(buildSearchParams(scopeFilter, page - 1), {
        replace: true,
      });
    }
  }, [bases.data, page, scopeFilter, setSearchParams]);

  const goToPage = (nextPage: number) => {
    setSearchParams(buildSearchParams(scopeFilter, nextPage), {
      replace: true,
    });
  };

  const handleScopeChange = (nextValue: string) => {
    const nextScope: ScopeFilterValue =
      nextValue === "company" ||
      nextValue === "department" ||
      nextValue === "personal"
        ? nextValue
        : "all";
    // 切换三层范围筛选时重置到第 1 页并同步 URL ?scope=。
    setSearchParams(buildSearchParams(nextScope, 1), { replace: true });
  };

  const handleDeleted = () => {
    if (page > 1 && bases.data?.items.length === 1) {
      goToPage(page - 1);
    }
    setDeleteBase(null);
  };

  const activeFilterLabel =
    SCOPE_FILTER_OPTIONS.find((item) => item.value === scopeFilter)?.label ??
    "全部";

  return (
    <div className="page-shell knowledge-page">
      <header className="page-heading">
        <div className="page-heading-copy">
          <h1>知识库</h1>
          <p className="page-description">
            管理全公司、部门共享与个人可检索文档资料。只读知识库可在对话中直接检索，仅对应管理员可编辑维护。
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

      <div className="flex flex-wrap items-center justify-between gap-3">
        <Tabs
          onValueChange={handleScopeChange}
          value={scopeFilter}
          variant="segmented"
        >
          <TabsList aria-label="按可见范围筛选知识库">
            {SCOPE_FILTER_OPTIONS.map((option) => (
              <TabsTrigger key={option.value} value={option.value}>
                {option.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      </div>

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
            scopeFilter === "all" ? (
              <button
                className="button-primary"
                onClick={() => setNewBaseOpen(true)}
                type="button"
              >
                <Icon name="plus" size={14} />
                新建知识库
              </button>
            ) : (
              <button
                className="button-quiet"
                onClick={() => handleScopeChange("all")}
                type="button"
              >
                查看全部知识库
              </button>
            )
          }
          description={
            scopeFilter === "all"
              ? "创建知识库后，可以上传文件或粘贴文本，再在对话与项目中使用。"
              : `当前「${activeFilterLabel}」范围内没有可访问的知识库。`
          }
          icon="book"
          title={
            scopeFilter === "all"
              ? "还没有知识库"
              : `暂无${activeFilterLabel}知识库`
          }
        />
      ) : (
        <section aria-labelledby="knowledge-list-title">
          <div className="surface-header knowledge-list-header">
            <div className="surface-title">
              <strong id="knowledge-list-title">
                {scopeFilter === "all"
                  ? "全部知识库"
                  : `${activeFilterLabel}知识库`}
              </strong>
              <span>按后端更新时间倒序排列</span>
            </div>
            <span className="knowledge-page-count">第 {page} 页</span>
          </div>

          <ul className="knowledge-card-grid">
            {bases.data.items.map((base) => {
              const scope = knowledgeBaseScope(base);
              const departmentName = resolveUserDepartmentName(
                user,
                base.owner_department_id,
              );
              const extraGrantCount = countExtraVisibilityGrants(base);
              const manageable = canManageKnowledgeBase(user, base);
              return (
                <li className="knowledge-card" key={base.id}>
                  <div className="knowledge-card-main">
                    <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
                      <VisibilityBadge
                        departmentName={departmentName}
                        scope={scope}
                      />
                      {extraGrantCount > 0 ? (
                        <Badge variant="info">
                          额外共享 {extraGrantCount} 项
                        </Badge>
                      ) : null}
                      {!manageable ? (
                        <Badge variant="neutral">只读</Badge>
                      ) : null}
                    </div>
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
                  {manageable ? (
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
                  ) : null}
                </li>
              );
            })}
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
