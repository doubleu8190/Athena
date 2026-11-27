import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";

import { parsePage } from "../../shared/lib/pagination";
import { Badge } from "../../shared/ui";
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
    <div className="page-shell admin-page admin-tools-page">
      <header className="page-heading">
        <div className="page-heading-copy">
          <h1>工具审批策略</h1>
          <p className="page-description">
            管理后端已注册 MCP 工具的审批开关与业务展示信息。保存按单个工具依次提交。
          </p>
        </div>
      </header>

      {tools.isPending ? (
        <div className="page-loading" role="status">
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
          className="admin-section"
        >
          <header className="admin-section-heading">
            <div>
              <h2 id="admin-tool-list-title">工具目录</h2>
            </div>
            <span className="admin-data-count">
              后端返回 {tools.data.total} 个工具
            </span>
          </header>

          {policy.error ? (
            <div className="admin-page-alert">
              <ErrorBanner
                message={`审批策略保存失败，界面已恢复服务端原状态。${adminErrorMessage(policy.error)}`}
              />
            </div>
          ) : policy.isPending ? (
            <p className="admin-form-status" role="status">
              正在逐项保存工具审批策略，其他开关暂时锁定…
            </p>
          ) : null}

          <ul className="admin-tool-list">
            {tools.data.items.map((tool) => (
              <li className="admin-tool-card" key={tool.tool_id}>
                <div className="admin-tool-main">
                  <div className="admin-title-line">
                    <h3>{tool.model_name}</h3>
                    {tool.display_name?.trim() ? (
                      <Badge variant="brand">{tool.display_name.trim()}</Badge>
                    ) : null}
                    <span
                      className={`admin-status-badge${
                        tool.requires_approval
                          ? " is-warning"
                          : " is-success"
                      }`}
                    >
                      {tool.requires_approval ? "需要审批" : "无需审批"}
                    </span>
                  </div>
                  <p className="admin-tool-description">
                    {tool.description || "后端未提供工具说明"}
                  </p>
                  <dl className="admin-detail-grid admin-tool-detail">
                    <div>
                      <dt>Tool ID</dt>
                      <dd className="admin-code-value">{tool.tool_id}</dd>
                    </div>
                    <div>
                      <dt>来源 Server</dt>
                      <dd className="admin-code-value">
                        {tool.server_id}
                      </dd>
                    </div>
                    <div>
                      <dt>远端名称</dt>
                      <dd className="admin-code-value">
                        {tool.remote_name}
                      </dd>
                    </div>
                    {tool.approval_summary_template?.trim() ? (
                      <div className="admin-detail-wide">
                        <dt>审批摘要模板</dt>
                        <dd>{tool.approval_summary_template.trim()}</dd>
                      </div>
                    ) : null}
                  </dl>
                </div>

                <div className="admin-tool-switch">
                  <button
                    aria-checked={tool.requires_approval}
                    aria-label={`${tool.model_name} 需要审批`}
                    className={`admin-switch${
                      tool.requires_approval ? " is-on" : ""
                    }`}
                    disabled={policy.isPending}
                    onClick={() => policy.update(tool)}
                    role="switch"
                    type="button"
                  >
                    <span aria-hidden="true" />
                  </button>
                  <small>
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
