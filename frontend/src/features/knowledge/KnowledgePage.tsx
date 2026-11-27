import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import { parsePage } from "../../shared/lib/pagination";
import {
  Badge,
  Button,
  LIST_FOOTER_CLASS,
  PAGE_DESC_CLASS,
  PAGE_HEADING_CLASS,
  PAGE_HEADING_COPY_CLASS,
  PAGE_LOADING_CLASS,
  PAGE_NAV_BUTTON_CLASS,
  PAGE_SHELL_CLASS,
  PAGE_TITLE_CLASS,
  Tabs,
  TabsList,
  TabsTrigger,
  VisibilityBadge,
  cn,
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

/** 解析知识库目录页的三层可见性筛选参数 ?scope=。 */
function parseScopeFilter(searchParams: URLSearchParams): ScopeFilterValue {
  const raw = searchParams.get("scope");
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
    <div className={PAGE_SHELL_CLASS}>
      <header className={PAGE_HEADING_CLASS}>
        <div className={PAGE_HEADING_COPY_CLASS}>
          <h1 className={PAGE_TITLE_CLASS}>知识库</h1>
          <p className={PAGE_DESC_CLASS}>
            管理全公司、部门共享与个人可检索文档资料。只读知识库可在对话中直接检索，仅对应管理员可编辑维护。
          </p>
        </div>
        <Button
          onClick={() => setNewBaseOpen(true)}
          type="button"
          variant="primary"
        >
          <Icon name="plus" size={14} />
          新建知识库
        </Button>
      </header>

      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
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
        <div className={PAGE_LOADING_CLASS} role="status">
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
              <Button
                onClick={() => setNewBaseOpen(true)}
                type="button"
                variant="primary"
              >
                <Icon name="plus" size={14} />
                新建知识库
              </Button>
            ) : (
              <Button
                onClick={() => handleScopeChange("all")}
                type="button"
                variant="outline"
              >
                查看全部知识库
              </Button>
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
          {/* 非卡片内部副表头去除左右内边距，与主标题和下方卡片网格左右齐平 */}
          <div className="mb-[13px] flex min-h-[58px] items-center justify-between gap-[18px] border-b border-border-subtle px-0.5 pb-3.5">
            <div className="flex min-w-0 flex-col gap-0.5">
              <strong
                className="text-[13px] font-semibold text-text-primary"
                id="knowledge-list-title"
              >
                {scopeFilter === "all"
                  ? "全部知识库"
                  : `${activeFilterLabel}知识库`}
              </strong>
              <span className="text-xs text-text-tertiary">
                按后端更新时间倒序排列
              </span>
            </div>
            <span className="font-mono text-xs text-text-tertiary">
              第 {page} 页
            </span>
          </div>

          <ul className="m-0 grid list-none grid-cols-1 gap-3 p-0 min-[1181px]:grid-cols-2">
            {bases.data.items.map((base) => {
              const scope = base.visibility_scope;
              const departmentName = resolveUserDepartmentName(
                user,
                base.owner_department_id,
              );
              const extraGrantCount = countExtraVisibilityGrants(base);
              const manageable = canManageKnowledgeBase(user, base);
              return (
                <li
                  className="flex min-h-[220px] min-w-0 flex-col rounded-xl border border-border-default bg-canvas p-4 shadow-xs transition-[border-color,box-shadow,transform] duration-150 hover:-translate-y-px hover:border-brand-border hover:shadow-sm min-[421px]:min-h-[240px] min-[421px]:p-[19px]"
                  key={base.id}
                >
                  <div className="min-w-0">
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
                      className="flex items-center justify-between gap-4 text-text-primary transition-colors hover:text-brand [&>svg]:shrink-0 [&>svg]:text-brand-hover"
                      to={`/knowledge/${encodeURIComponent(base.id)}`}
                    >
                      <strong className="min-w-0 truncate text-lg font-semibold tracking-[-0.02em]">
                        {base.name}
                      </strong>
                      <Icon name="arrow-right" size={16} />
                    </Link>
                    <p className="mt-2 line-clamp-3 text-sm leading-[1.65] text-text-secondary">
                      {base.description || "未填写说明"}
                    </p>
                  </div>
                  <dl className="mt-auto grid grid-cols-1 gap-2.5 pt-[18px] min-[421px]:grid-cols-2 [&>div]:min-w-0 [&_dt]:font-mono [&_dt]:text-xs [&_dt]:tracking-[0.06em] [&_dt]:text-text-tertiary [&_dt]:uppercase [&_dd]:mt-[3px] [&_dd]:font-mono [&_dd]:text-xs [&_dd]:text-text-secondary">
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
                    <div className="mt-[15px] flex flex-wrap gap-[7px] border-t border-border-subtle pt-3.5 [&>*]:flex-[1_1_120px] min-[701px]:[&>*]:flex-1">
                      <Button
                        aria-label={`编辑知识库：${base.name}`}
                        onClick={() => setEditBase(base)}
                        size="sm"
                        type="button"
                        variant="outline"
                      >
                        <Icon name="edit" size={14} />
                        编辑
                      </Button>
                      <Button
                        aria-label={`删除知识库：${base.name}`}
                        onClick={() => setDeleteBase(base)}
                        size="sm"
                        type="button"
                        variant="danger-outline"
                      >
                        <Icon name="trash" size={14} />
                        删除
                      </Button>
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>

          <footer
            className={cn(
              LIST_FOOTER_CLASS,
              "mt-3 rounded-[10px] border border-border-default",
            )}
          >
            <span>
              本页 {bases.data.items.length} 个知识库
              {bases.data.items.length >= responsePageSize
                ? "（可能还有下一页）"
                : ""}
            </span>
            <nav className="flex items-center gap-2" aria-label="知识库分页">
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
