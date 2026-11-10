import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import {
  AdminPagination,
  lastPageNumber,
} from "./AdminPagination";
import { McpServerDialog } from "./McpServerDialog";
import {
  discoveredAtLabel,
  mcpConfigSummary,
  mcpTransportLabel,
  secretMetadataLabel,
} from "./display";
import { adminErrorMessage } from "./errorMessages";
import { useMcpServers } from "./hooks";

const PAGE_SIZE = 10;

function parsePage(value: string | null): number {
  if (!value) {
    return 1;
  }
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 1;
}

export function AdminMcpPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const page = parsePage(searchParams.get("page"));
  const servers = useMcpServers({ page, pageSize: PAGE_SIZE });
  const [createOpen, setCreateOpen] = useState(false);
  const [editServerId, setEditServerId] = useState<string | null>(null);
  const lastPage =
    servers.isSuccess
      ? lastPageNumber(
          servers.data.total,
          servers.data.page_size,
        )
      : 1;

  useEffect(() => {
    if (servers.isSuccess && page > lastPage) {
      setSearchParams(
        lastPage > 1 ? { page: String(lastPage) } : {},
        { replace: true },
      );
    }
  }, [lastPage, page, servers.isSuccess, setSearchParams]);

  const goToPage = (nextPage: number) => {
    setSearchParams(
      nextPage > 1 ? { page: String(nextPage) } : {},
      { replace: true },
    );
  };

  return (
    <div className="page-shell admin-page admin-mcp-page">
      <header className="page-heading">
        <div className="page-heading-copy">
          <p className="page-eyebrow">Admin / MCP</p>
          <h1>MCP Server</h1>
          <p className="page-description">
            管理全局 MCP 连接。创建和编辑都会由后端真实连接并发现工具，页面不模拟连接或发现结果。
          </p>
        </div>
        <button
          className="button-primary"
          onClick={() => setCreateOpen(true)}
          type="button"
        >
          <Icon name="plus" size={14} />
          新建 MCP Server
        </button>
      </header>

      {servers.isPending ? (
        <div className="page-loading" role="status">
          正在读取 MCP Server…
        </div>
      ) : servers.isError ? (
        <ErrorBanner
          message={adminErrorMessage(servers.error)}
          onRetry={() => void servers.refetch()}
        />
      ) : servers.data.items.length === 0 && page === 1 ? (
        <EmptyState
          action={
            <button
              className="button-primary"
              onClick={() => setCreateOpen(true)}
              type="button"
            >
              新建第一个 MCP Server
            </button>
          }
          description="新建时后端会连接远端 Server、发现工具并原子写入目录；失败不会创建占位配置。"
          icon="settings"
          kicker="No MCP servers"
          title="还没有 MCP Server"
        />
      ) : (
        <section
          aria-labelledby="mcp-server-list-title"
          className="admin-section"
        >
          <header className="admin-section-heading">
            <div>
              <p className="admin-section-kicker">Global connections</p>
              <h2 id="mcp-server-list-title">Server 目录</h2>
            </div>
            <span className="admin-data-count">
              后端返回 {servers.data.total} 个 Server
            </span>
          </header>

          <ul className="admin-server-list">
            {servers.data.items.map((server) => (
              <li className="admin-server-card" key={server.id}>
                <div className="admin-card-heading">
                  <div>
                    <div className="admin-title-line">
                      <h3>{server.name}</h3>
                      <span className="admin-status-badge is-accent">
                        {mcpTransportLabel(server)}
                      </span>
                      <span className="admin-status-badge">
                        v{server.config_version}
                      </span>
                    </div>
                    <p className="admin-muted">
                      {discoveredAtLabel(server)}
                    </p>
                  </div>
                  <button
                    aria-label={`编辑 MCP Server：${server.name}`}
                    className="button-quiet"
                    onClick={() => setEditServerId(server.id)}
                    type="button"
                  >
                    <Icon name="edit" size={14} />
                    编辑
                  </button>
                </div>

                <div className="admin-server-config">
                  <span>连接配置</span>
                  <code>{mcpConfigSummary(server)}</code>
                </div>
                <dl className="admin-detail-grid">
                  <div className="admin-detail-wide">
                    <dt>秘密名称与保存状态</dt>
                    <dd>{secretMetadataLabel(server)}</dd>
                  </div>
                  <div>
                    <dt>创建者</dt>
                    <dd className="admin-code-value">
                      {server.created_by ?? "后端未返回"}
                    </dd>
                  </div>
                  <div>
                    <dt>创建时间</dt>
                    <dd>{formatDateTime(server.created_at)}</dd>
                  </div>
                  <div>
                    <dt>更新时间</dt>
                    <dd>{formatDateTime(server.updated_at)}</dd>
                  </div>
                  <div>
                    <dt>工具发现时间</dt>
                    <dd>
                      {server.tools_discovered_at
                        ? formatDateTime(server.tools_discovered_at)
                        : "尚无成功发现时间"}
                    </dd>
                  </div>
                </dl>
                <p className="admin-card-footnote">
                  后端未提供“重新发现”接口；编辑并完整替换配置时会重新连接并发现工具。
                </p>
              </li>
            ))}
          </ul>

          <AdminPagination
            label="MCP Server 分页"
            onPageChange={goToPage}
            page={page}
            pageSize={servers.data.page_size}
            total={servers.data.total}
          />
        </section>
      )}

      <McpServerDialog
        onClose={() => setCreateOpen(false)}
        open={createOpen}
      />
      {editServerId ? (
        <McpServerDialog
          onClose={() => setEditServerId(null)}
          open
          serverId={editServerId}
        />
      ) : null}
    </div>
  );
}
