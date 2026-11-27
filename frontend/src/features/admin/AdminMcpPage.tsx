import { Pencil, Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import { parsePage } from "../../shared/lib/pagination";
import {
  ADMIN_CARD_CLASS,
  ADMIN_CARD_HEADING_CLASS,
  ADMIN_CARD_LIST_CLASS,
  ADMIN_CODE_VALUE_CLASS,
  ADMIN_DATA_COUNT_CLASS,
  ADMIN_DETAIL_GRID_CLASS,
  ADMIN_DETAIL_WIDE_CLASS,
  ADMIN_MUTED_CLASS,
  ADMIN_SECTION_CLASS,
  ADMIN_SECTION_HEADING_CLASS,
  ADMIN_TITLE_LINE_CLASS,
  Badge,
  Button,
  InlineAlert,
  PAGE_DESC_CLASS,
  PAGE_HEADING_CLASS,
  PAGE_HEADING_COPY_CLASS,
  PAGE_LOADING_CLASS,
  PAGE_SHELL_CLASS,
  PAGE_TITLE_CLASS,
  TEXT_BUTTON_CLASS,
  VisibilityBadge,
  cn,
} from "../../shared/ui";
import { EmptyState } from "../../shared/ui/EmptyState";
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
    <div className={cn(PAGE_SHELL_CLASS, "min-w-0")}>
      <header className={PAGE_HEADING_CLASS}>
        <div className={PAGE_HEADING_COPY_CLASS}>
          <h1 className={PAGE_TITLE_CLASS}>MCP Server</h1>
          <p className={PAGE_DESC_CLASS}>
            管理企业 MCP 连接。创建和编辑都会由后端真实连接并发现工具，页面不模拟连接或发现结果。
          </p>
        </div>
        <Button
          onClick={() => setCreateOpen(true)}
          type="button"
          variant="primary"
        >
          <Plus aria-hidden="true" focusable="false" size={14} strokeWidth={1.7} />
          新建 MCP Server
        </Button>
      </header>

      {servers.isPending ? (
        <div className={PAGE_LOADING_CLASS} role="status">
          正在读取 MCP Server…
        </div>
      ) : servers.isError ? (
        <InlineAlert
          action={
            <button
              className={TEXT_BUTTON_CLASS}
              onClick={() => void servers.refetch()}
              type="button"
            >
              重试
            </button>
          }
          size="compact"
          variant="danger"
        >
          {adminErrorMessage(servers.error)}
        </InlineAlert>
      ) : servers.data.items.length === 0 && page === 1 ? (
        <EmptyState
          action={
            <Button
              onClick={() => setCreateOpen(true)}
              type="button"
              variant="primary"
            >
              新建第一个 MCP Server
            </Button>
          }
          description="新建时后端会连接远端 Server、发现工具并原子写入目录；失败不会创建占位配置。"
          icon="settings"
          title="还没有 MCP Server"
        />
      ) : (
        <section
          aria-labelledby="mcp-server-list-title"
          className={ADMIN_SECTION_CLASS}
        >
          <header className={ADMIN_SECTION_HEADING_CLASS}>
            <div>
              <h2 id="mcp-server-list-title">Server 目录</h2>
            </div>
            <span className={ADMIN_DATA_COUNT_CLASS}>
              后端返回 {servers.data.total} 个 Server
            </span>
          </header>

          <ul className={ADMIN_CARD_LIST_CLASS}>
            {servers.data.items.map((server) => (
              <li className={ADMIN_CARD_CLASS} key={server.id}>
                <div className={ADMIN_CARD_HEADING_CLASS}>
                  <div className="min-w-0">
                    <div className={ADMIN_TITLE_LINE_CLASS}>
                      <h3>{server.name}</h3>
                      <Badge className="rounded-full" variant="brand">
                        {mcpTransportLabel(server)}
                      </Badge>
                      <Badge className="rounded-full" variant="neutral">
                        v{server.config_version}
                      </Badge>
                      <Badge
                        className="rounded-full"
                        variant={server.enabled ? "success" : "neutral"}
                      >
                        {server.enabled ? "已启用" : "已停用"}
                      </Badge>
                      <VisibilityBadge
                        departmentName={
                          server.visibility_scope === "department"
                            ? server.owner_department_id
                            : null
                        }
                        scope={server.visibility_scope}
                      />
                    </div>
                    <p className={ADMIN_MUTED_CLASS}>
                      {discoveredAtLabel(server)}
                    </p>
                  </div>
                  <Button
                    aria-label={`编辑 MCP Server：${server.name}`}
                    onClick={() => setEditServerId(server.id)}
                    size="sm"
                    type="button"
                    variant="outline"
                  >
                    <Pencil
                      aria-hidden="true"
                      focusable="false"
                      size={14}
                      strokeWidth={1.7}
                    />
                    编辑
                  </Button>
                </div>

                <div className="mt-3.5 flex min-w-0 flex-col items-start gap-1 rounded-[9px] border border-border-default bg-subtle px-3 py-[11px] sm:flex-row sm:items-baseline sm:gap-2.5">
                  <span className="shrink-0 text-xs text-text-tertiary">
                    连接配置
                  </span>
                  <code className="min-w-0 font-mono text-xs leading-[1.6] text-text-primary [overflow-wrap:anywhere]">
                    {mcpConfigSummary(server)}
                  </code>
                </div>
                <dl className={ADMIN_DETAIL_GRID_CLASS}>
                  <div className={ADMIN_DETAIL_WIDE_CLASS}>
                    <dt>秘密名称与保存状态</dt>
                    <dd>{secretMetadataLabel(server)}</dd>
                  </div>
                  <div>
                    <dt>创建者</dt>
                    <dd className={ADMIN_CODE_VALUE_CLASS}>
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
                <p
                  className={cn(
                    ADMIN_MUTED_CLASS,
                    "mt-3 border-t border-dashed border-border-default pt-2.5",
                  )}
                >
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
