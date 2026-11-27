import { Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import { parsePage } from "../../shared/lib/pagination";
import {
  ADMIN_CARD_ACTIONS_CLASS,
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
  CONFIRM_COPY_CLASS,
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
import { Modal } from "../../shared/ui/Modal";
import { useAuth } from "../auth/AuthProvider";
import { isSuperAdmin } from "../auth/model";
import { AdminPagination, lastPageNumber } from "./AdminPagination";
import { resolveAdminDepartmentName } from "./AdminVisibilityFields";
import { McpServerDialog } from "./McpServerDialog";
import {
  discoveredAtLabel,
  mcpConfigSummary,
  mcpTransportLabel,
  secretMetadataLabel,
} from "./display";
import { adminErrorMessage } from "./errorMessages";
import {
  useAdminOrgDepartments,
  useDeleteMcpServerMutation,
  useMcpServers,
  useRediscoverMcpServerMutation,
  useSetMcpServerEnabledMutation,
} from "./hooks";
import type { McpServer } from "./model";

const PAGE_SIZE = 10;

export function AdminMcpPage() {
  const { user } = useAuth();
  const superAdmin = isSuperAdmin(user);
  const [searchParams, setSearchParams] = useSearchParams();
  const page = parsePage(searchParams.get("page"));
  const servers = useMcpServers({ page, pageSize: PAGE_SIZE });
  const hasDepartmentServer =
    servers.data?.items.some((item) => item.visibility_scope === "department") ??
    false;
  // 仅当列表实际存在部门共享 Server 时按需查询组织部门目录，避免公司级目录产生多余请求。
  const departmentsQuery = useAdminOrgDepartments(
    superAdmin && hasDepartmentServer,
    false,
  );
  const toggleEnabledMutation = useSetMcpServerEnabledMutation();
  const rediscoverMutation = useRediscoverMcpServerMutation();
  const deleteMutation = useDeleteMcpServerMutation();

  const [createOpen, setCreateOpen] = useState(false);
  const [editServerId, setEditServerId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<McpServer | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const lastPage = servers.isSuccess
    ? lastPageNumber(servers.data.total, servers.data.page_size)
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

  const handleToggleEnabled = async (server: McpServer) => {
    setActionError(null);
    try {
      await toggleEnabledMutation.mutateAsync({
        serverId: server.id,
        input: {
          enabled: !server.enabled,
          expected_config_version: server.config_version,
        },
      });
    } catch (error) {
      setActionError(adminErrorMessage(error));
    }
  };

  const handleRediscover = async (server: McpServer) => {
    setActionError(null);
    try {
      await rediscoverMutation.mutateAsync(server.id);
    } catch (error) {
      setActionError(adminErrorMessage(error));
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) {
      return;
    }
    setDeleteError(null);
    try {
      await deleteMutation.mutateAsync(deleteTarget.id);
      setDeleteTarget(null);
    } catch (error) {
      setDeleteError(adminErrorMessage(error));
    }
  };

  const activeDepartments = departmentsQuery.data?.items;

  return (
    <div className={cn(PAGE_SHELL_CLASS, "min-w-0")}>
      <header className={PAGE_HEADING_CLASS}>
        <div className={PAGE_HEADING_COPY_CLASS}>
          <h1 className={PAGE_TITLE_CLASS}>MCP Server</h1>
          <p className={PAGE_DESC_CLASS}>
            管理企业 MCP 连接、启停状态、工具重发现与可见范围权限。页面只展示后端安全投影，凭据不回显明文值。
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

      {actionError ? (
        <div className="mb-4">
          <InlineAlert
            action={
              <button
                className={TEXT_BUTTON_CLASS}
                onClick={() => {
                  setActionError(null);
                  void servers.refetch();
                }}
                type="button"
              >
                刷新列表
              </button>
            }
            size="compact"
            variant="danger"
          >
            {actionError}
          </InlineAlert>
        </div>
      ) : null}

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
            {servers.data.items.map((server) => {
              const toggling =
                toggleEnabledMutation.isPending &&
                toggleEnabledMutation.variables?.serverId === server.id;
              const rediscovering =
                rediscoverMutation.isPending &&
                rediscoverMutation.variables === server.id;
              const cardBusy =
                toggleEnabledMutation.isPending ||
                rediscoverMutation.isPending ||
                deleteMutation.isPending;
              const departmentName =
                server.visibility_scope === "department"
                  ? resolveAdminDepartmentName(
                      user,
                      activeDepartments,
                      server.owner_department_id,
                    )
                  : null;

              return (
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
                          departmentName={departmentName}
                          scope={server.visibility_scope}
                        />
                      </div>
                      <p className={ADMIN_MUTED_CLASS}>
                        {discoveredAtLabel(server)}
                      </p>
                    </div>
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

                  <footer className={cn(ADMIN_CARD_ACTIONS_CLASS, "flex-wrap")}>
                    <Button
                      aria-label={`${server.enabled ? "停用" : "启用"} MCP Server：${server.name}`}
                      aria-pressed={server.enabled}
                      disabled={cardBusy}
                      onClick={() => void handleToggleEnabled(server)}
                      size="sm"
                      type="button"
                      variant="outline"
                    >
                      {toggling
                        ? "处理中…"
                        : server.enabled
                          ? "停用"
                          : "启用"}
                    </Button>
                    <Button
                      aria-label={`重新发现工具：${server.name}`}
                      disabled={cardBusy}
                      onClick={() => void handleRediscover(server)}
                      size="sm"
                      type="button"
                      variant="outline"
                    >
                      <RefreshCw
                        aria-hidden="true"
                        className={cn(rediscovering && "animate-spin")}
                        focusable="false"
                        size={14}
                        strokeWidth={1.7}
                      />
                      {rediscovering ? "正在发现…" : "重新发现工具"}
                    </Button>
                    <Button
                      aria-label={`编辑 MCP Server：${server.name}`}
                      disabled={cardBusy}
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
                    <Button
                      aria-label={`删除 MCP Server：${server.name}`}
                      disabled={cardBusy}
                      onClick={() => {
                        setDeleteError(null);
                        setDeleteTarget(server);
                      }}
                      size="sm"
                      type="button"
                      variant="danger-outline"
                    >
                      <Trash2
                        aria-hidden="true"
                        focusable="false"
                        size={14}
                        strokeWidth={1.7}
                      />
                      删除
                    </Button>
                  </footer>
                </li>
              );
            })}
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

      <Modal
        closeDisabled={deleteMutation.isPending}
        description="删除后该 MCP Server 及其注册的工具将从目录中移除，历史运行记录不受影响。"
        footer={
          <>
            <Button
              disabled={deleteMutation.isPending}
              onClick={() => setDeleteTarget(null)}
              type="button"
              variant="outline"
            >
              取消
            </Button>
            <Button
              disabled={deleteMutation.isPending}
              onClick={() => void handleDelete()}
              type="button"
              variant="danger"
            >
              {deleteMutation.isPending ? "正在删除…" : "确认删除"}
            </Button>
          </>
        }
        onClose={() => {
          if (!deleteMutation.isPending) {
            setDeleteTarget(null);
          }
        }}
        open={Boolean(deleteTarget)}
        title={`删除 MCP Server「${deleteTarget?.name ?? ""}」`}
      >
        <p className={CONFIRM_COPY_CLASS}>
          确认删除 MCP Server <code>{deleteTarget?.name}</code>（v
          {deleteTarget?.config_version}）吗？
        </p>
        {deleteError ? (
          <div className="mt-3">
            <InlineAlert size="compact" variant="danger">
              {deleteError}
            </InlineAlert>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
