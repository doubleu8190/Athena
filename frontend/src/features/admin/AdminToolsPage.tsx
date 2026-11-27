import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";

import { parsePage } from "../../shared/lib/pagination";
import {
  ADMIN_CARD_CLASS,
  ADMIN_CARD_LIST_CLASS,
  ADMIN_CODE_VALUE_CLASS,
  ADMIN_DATA_COUNT_CLASS,
  ADMIN_DETAIL_GRID_CLASS,
  ADMIN_DETAIL_WIDE_CLASS,
  ADMIN_SECTION_CLASS,
  ADMIN_SECTION_HEADING_CLASS,
  ADMIN_TITLE_LINE_CLASS,
  Badge,
  NOTICE_BRAND_CLASS,
  PAGE_DESC_CLASS,
  PAGE_HEADING_CLASS,
  PAGE_HEADING_COPY_CLASS,
  PAGE_LOADING_CLASS,
  PAGE_SHELL_CLASS,
  PAGE_TITLE_CLASS,
  Switch,
  cn,
} from "../../shared/ui";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import {
  AdminPagination,
  lastPageNumber,
} from "./AdminPagination";
import { adminErrorMessage } from "./errorMessages";
import {
  useAdminTools,
  useUpdateToolApprovalForRow,
} from "./hooks";

const PAGE_SIZE = 20;

export function AdminToolsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const page = parsePage(searchParams.get("page"));
  const tools = useAdminTools({ page, pageSize: PAGE_SIZE });
  const policy = useUpdateToolApprovalForRow();
  const lastPage =
    tools.isSuccess
      ? lastPageNumber(tools.data.total, tools.data.page_size)
      : 1;

  useEffect(() => {
    if (tools.isSuccess && page > lastPage) {
      setSearchParams(
        lastPage > 1 ? { page: String(lastPage) } : {},
        { replace: true },
      );
    }
  }, [lastPage, page, setSearchParams, tools.isSuccess]);

  const goToPage = (nextPage: number) => {
    setSearchParams(
      nextPage > 1 ? { page: String(nextPage) } : {},
      { replace: true },
    );
  };

  return (
    <div className={cn(PAGE_SHELL_CLASS, "min-w-0")}>
      <header className={PAGE_HEADING_CLASS}>
        <div className={PAGE_HEADING_COPY_CLASS}>
          <h1 className={PAGE_TITLE_CLASS}>工具审批策略</h1>
          <p className={PAGE_DESC_CLASS}>
            管理后端已注册 MCP 工具的审批开关与业务展示信息。保存按单个工具依次提交。
          </p>
        </div>
      </header>

      {tools.isPending ? (
        <div className={PAGE_LOADING_CLASS} role="status">
          正在读取 MCP 工具目录…
        </div>
      ) : tools.isError ? (
        <ErrorBanner
          message={adminErrorMessage(tools.error)}
          onRetry={() => void tools.refetch()}
        />
      ) : tools.data.items.length === 0 && page === 1 ? (
        <EmptyState
          description="工具只会由后端在 MCP Server 创建或完整替换并成功发现后注册。当前没有可管理的工具。"
          icon="tools"
          title="还没有已注册工具"
        />
      ) : (
        <section
          aria-labelledby="admin-tool-list-title"
          className={ADMIN_SECTION_CLASS}
        >
          <header className={ADMIN_SECTION_HEADING_CLASS}>
            <div>
              <h2 id="admin-tool-list-title">工具目录</h2>
            </div>
            <span className={ADMIN_DATA_COUNT_CLASS}>
              后端返回 {tools.data.total} 个工具
            </span>
          </header>

          {policy.error ? (
            <div className="mb-4">
              <ErrorBanner
                message={`审批策略保存失败，界面已恢复服务端原状态。${adminErrorMessage(policy.error)}`}
              />
            </div>
          ) : policy.isPending ? (
            <p className={cn(NOTICE_BRAND_CLASS, "mb-3")} role="status">
              正在逐项保存工具审批策略，其他开关暂时锁定…
            </p>
          ) : null}

          <ul className={ADMIN_CARD_LIST_CLASS}>
            {tools.data.items.map((tool) => (
              <li
                className={cn(
                  ADMIN_CARD_CLASS,
                  "flex flex-col items-start justify-between gap-4 sm:flex-row lg:gap-6",
                )}
                key={tool.tool_id}
              >
                <div className="min-w-0 flex-1">
                  <div className={ADMIN_TITLE_LINE_CLASS}>
                    <h3>{tool.model_name}</h3>
                    {tool.display_name?.trim() ? (
                      <Badge variant="brand">{tool.display_name.trim()}</Badge>
                    ) : null}
                    <Badge
                      className="rounded-full"
                      variant={tool.requires_approval ? "warning" : "success"}
                    >
                      {tool.requires_approval ? "需要审批" : "无需审批"}
                    </Badge>
                  </div>
                  <p className="mt-[7px] max-w-[760px] text-sm leading-[1.65] text-text-secondary">
                    {tool.description || "后端未提供工具说明"}
                  </p>
                  <dl
                    className={cn(
                      ADMIN_DETAIL_GRID_CLASS,
                      "sm:grid-cols-1 lg:grid-cols-3",
                    )}
                  >
                    <div>
                      <dt>Tool ID</dt>
                      <dd className={ADMIN_CODE_VALUE_CLASS}>{tool.tool_id}</dd>
                    </div>
                    <div>
                      <dt>来源 Server</dt>
                      <dd className={ADMIN_CODE_VALUE_CLASS}>
                        {tool.server_id}
                      </dd>
                    </div>
                    <div>
                      <dt>远端名称</dt>
                      <dd className={ADMIN_CODE_VALUE_CLASS}>
                        {tool.remote_name}
                      </dd>
                    </div>
                    {tool.approval_summary_template?.trim() ? (
                      <div className={ADMIN_DETAIL_WIDE_CLASS}>
                        <dt>审批摘要模板</dt>
                        <dd>{tool.approval_summary_template.trim()}</dd>
                      </div>
                    ) : null}
                  </dl>
                </div>

                <div className="flex w-full shrink-0 flex-row items-center justify-end gap-1.5 border-t border-border-subtle pt-2.5 sm:w-auto sm:flex-col sm:border-t-0 sm:pt-0.5">
                  <Switch
                    aria-label={`${tool.model_name} 需要审批`}
                    checked={tool.requires_approval}
                    disabled={policy.isPending}
                    onCheckedChange={() => policy.update(tool)}
                  />
                  <small className="text-xs text-text-tertiary">
                    {policy.isPending &&
                    policy.pendingToolId === tool.tool_id
                      ? "正在保存"
                      : "管理员审批策略"}
                  </small>
                </div>
              </li>
            ))}
          </ul>

          <AdminPagination
            label="MCP 工具分页"
            onPageChange={goToPage}
            page={page}
            pageSize={tools.data.page_size}
            total={tools.data.total}
          />
        </section>
      )}
    </div>
  );
}
