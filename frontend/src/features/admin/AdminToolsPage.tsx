import { Pencil } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { getAuthIdentityEpoch } from "../../shared/api/client";
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
  Button,
  Checkbox,
  InlineAlert,
  NOTICE_BRAND_CLASS,
  PAGE_DESC_CLASS,
  PAGE_HEADING_CLASS,
  PAGE_HEADING_COPY_CLASS,
  PAGE_LOADING_CLASS,
  PAGE_SHELL_CLASS,
  PAGE_TITLE_CLASS,
  Switch,
  TEXT_BUTTON_CLASS,
  cn,
} from "../../shared/ui";
import { EmptyState } from "../../shared/ui/EmptyState";
import { AdminPagination, lastPageNumber } from "./AdminPagination";
import { ToolMetadataDialog } from "./ToolMetadataDialog";
import { adminErrorMessage } from "./errorMessages";
import {
  useAdminTools,
  useBatchUpdateToolApprovalMutation,
  useUpdateToolApprovalForRow,
} from "./hooks";
import type { AdminTool } from "./model";

const PAGE_SIZE = 20;

export function AdminToolsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const page = parsePage(searchParams.get("page"));
  const tools = useAdminTools({ page, pageSize: PAGE_SIZE });
  const policy = useUpdateToolApprovalForRow();
  const batchMutation = useBatchUpdateToolApprovalMutation();
  const [selectedToolIds, setSelectedToolIds] = useState<string[]>([]);
  const [batchError, setBatchError] = useState<string | null>(null);
  const [editingToolId, setEditingToolId] = useState<string | null>(null);

  const lastPage = tools.isSuccess
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

  // 翻页或列表刷新后仅保留当前页仍存在的 tool_id，避免跨页或已删除项误入批量提交。
  const currentPageIds = useMemo(
    () => (tools.data?.items ?? []).map((item) => item.tool_id),
    [tools.data?.items],
  );
  const visibleSelectedIds = useMemo(() => {
    const validSet = new Set(currentPageIds);
    return selectedToolIds.filter((id) => validSet.has(id));
  }, [currentPageIds, selectedToolIds]);

  const allSelected =
    currentPageIds.length > 0 &&
    visibleSelectedIds.length === currentPageIds.length;
  const someSelected =
    visibleSelectedIds.length > 0 &&
    visibleSelectedIds.length < currentPageIds.length;

  const editingTool: AdminTool | null =
    tools.data?.items.find((item) => item.tool_id === editingToolId) ?? null;

  const controlsLocked = policy.isPending || batchMutation.isPending;

  const goToPage = (nextPage: number) => {
    setSelectedToolIds([]);
    setBatchError(null);
    setSearchParams(
      nextPage > 1 ? { page: String(nextPage) } : {},
      { replace: true },
    );
  };

  const toggleSelectAll = () => {
    setBatchError(null);
    if (allSelected) {
      setSelectedToolIds([]);
    } else {
      setSelectedToolIds(currentPageIds);
    }
  };

  const toggleSelectTool = (toolId: string) => {
    setBatchError(null);
    setSelectedToolIds((current) =>
      current.includes(toolId)
        ? current.filter((id) => id !== toolId)
        : [...current, toolId],
    );
  };

  const handleBatchApproval = async (requiresApproval: boolean) => {
    if (visibleSelectedIds.length === 0 || controlsLocked) {
      return;
    }
    setBatchError(null);
    try {
      await batchMutation.mutateAsync({
        identityEpoch: getAuthIdentityEpoch(),
        input: {
          requires_approval: requiresApproval,
          tool_ids: visibleSelectedIds,
        },
      });
      setSelectedToolIds([]);
    } catch (error) {
      setBatchError(adminErrorMessage(error));
    }
  };

  return (
    <div className={cn(PAGE_SHELL_CLASS, "min-w-0")}>
      <header className={PAGE_HEADING_CLASS}>
        <div className={PAGE_HEADING_COPY_CLASS}>
          <h1 className={PAGE_TITLE_CLASS}>工具审批策略</h1>
          <p className={PAGE_DESC_CLASS}>
            管理后端已注册 MCP 工具的审批开关、批量策略与业务展示信息。
          </p>
        </div>
      </header>

      {tools.isPending ? (
        <div className={PAGE_LOADING_CLASS} role="status">
          正在读取 MCP 工具目录…
        </div>
      ) : tools.isError ? (
        <InlineAlert
          action={
            <button
              className={TEXT_BUTTON_CLASS}
              onClick={() => void tools.refetch()}
              type="button"
            >
              重试
            </button>
          }
          size="compact"
          variant="danger"
        >
          {adminErrorMessage(tools.error)}
        </InlineAlert>
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

          {/* 批量选择与原子审批开关工具栏 */}
          <div className="mb-3.5 flex flex-wrap items-center justify-between gap-2.5 rounded-[10px] border border-border-default bg-subtle px-3 py-2.5">
            <div className="flex flex-wrap items-center gap-3">
              <label className="inline-flex cursor-pointer items-center gap-2 text-xs font-medium text-text-primary">
                <Checkbox
                  aria-label="全选当前页工具"
                  checked={
                    allSelected ? true : someSelected ? "indeterminate" : false
                  }
                  disabled={controlsLocked || currentPageIds.length === 0}
                  onCheckedChange={toggleSelectAll}
                />
                <span>全选当前页</span>
              </label>
              <span className="text-xs text-text-secondary">
                已选 {visibleSelectedIds.length} 项
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button
                disabled={visibleSelectedIds.length === 0 || controlsLocked}
                onClick={() => void handleBatchApproval(true)}
                size="sm"
                type="button"
                variant="outline"
              >
                批量开启审批
              </Button>
              <Button
                disabled={visibleSelectedIds.length === 0 || controlsLocked}
                onClick={() => void handleBatchApproval(false)}
                size="sm"
                type="button"
                variant="outline"
              >
                批量关闭审批
              </Button>
            </div>
          </div>

          {policy.error ? (
            <div className="mb-4">
              <InlineAlert size="compact" variant="danger">
                {`审批策略保存失败，界面已恢复服务端原状态。${adminErrorMessage(policy.error)}`}
              </InlineAlert>
            </div>
          ) : batchError ? (
            <div className="mb-4">
              <InlineAlert size="compact" variant="danger">
                {`批量审批策略保存失败。${batchError}`}
              </InlineAlert>
            </div>
          ) : policy.isPending ? (
            <p className={cn(NOTICE_BRAND_CLASS, "mb-3")} role="status">
              正在逐项保存工具审批策略，其他开关暂时锁定…
            </p>
          ) : batchMutation.isPending ? (
            <p className={cn(NOTICE_BRAND_CLASS, "mb-3")} role="status">
              正在批量保存工具审批策略，控件暂时锁定…
            </p>
          ) : null}

          <ul className={ADMIN_CARD_LIST_CLASS}>
            {tools.data.items.map((tool) => {
              const selected = visibleSelectedIds.includes(tool.tool_id);
              const paramLabelEntries = Object.entries(
                tool.parameter_labels ?? {},
              );
              const resultLabelEntries = Object.entries(
                tool.result_labels ?? {},
              );

              return (
                <li
                  className={cn(
                    ADMIN_CARD_CLASS,
                    "flex flex-col items-start justify-between gap-4 sm:flex-row lg:gap-6",
                    selected && "border-brand-border",
                  )}
                  key={tool.tool_id}
                >
                  <div className="flex min-w-0 flex-1 items-start gap-3">
                    <div className="pt-1">
                      <Checkbox
                        aria-label={`选择工具：${tool.model_name}`}
                        checked={selected}
                        disabled={controlsLocked}
                        onCheckedChange={() => toggleSelectTool(tool.tool_id)}
                      />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className={ADMIN_TITLE_LINE_CLASS}>
                        <h3>{tool.model_name}</h3>
                        {tool.display_name?.trim() ? (
                          <Badge variant="brand">
                            {tool.display_name.trim()}
                          </Badge>
                        ) : null}
                        <Badge
                          className="rounded-full"
                          variant={tool.requires_approval ? "warning" : "success"}
                        >
                          {tool.requires_approval ? "需要审批" : "无需审批"}
                        </Badge>
                      </div>
                      <p className="mt-[7px] max-w-[760px] text-sm leading-[1.65] text-text-secondary [overflow-wrap:anywhere]">
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
                          <dd className={ADMIN_CODE_VALUE_CLASS}>
                            {tool.tool_id}
                          </dd>
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
                            <dd className="[overflow-wrap:anywhere]">
                              {tool.approval_summary_template.trim()}
                            </dd>
                          </div>
                        ) : null}
                        {paramLabelEntries.length > 0 ? (
                          <div className={ADMIN_DETAIL_WIDE_CLASS}>
                            <dt>参数中文标签</dt>
                            <dd className="[overflow-wrap:anywhere]">
                              {paramLabelEntries
                                .map(([key, label]) => `${key}: ${label}`)
                                .join("、")}
                            </dd>
                          </div>
                        ) : null}
                        {resultLabelEntries.length > 0 ? (
                          <div className={ADMIN_DETAIL_WIDE_CLASS}>
                            <dt>可展示结果字段</dt>
                            <dd className="[overflow-wrap:anywhere]">
                              {resultLabelEntries
                                .map(([key, label]) => `${key}: ${label}`)
                                .join("、")}
                            </dd>
                          </div>
                        ) : null}
                      </dl>
                    </div>
                  </div>

                  <div className="flex w-full shrink-0 flex-wrap items-center justify-end gap-2.5 border-t border-border-subtle pt-2.5 sm:w-auto sm:flex-col sm:items-end sm:border-t-0 sm:pt-0.5">
                    <div className="flex items-center gap-2 sm:flex-col sm:items-end sm:gap-1">
                      <Switch
                        aria-label={`${tool.model_name} 需要审批`}
                        checked={tool.requires_approval}
                        disabled={controlsLocked}
                        onCheckedChange={() => policy.update(tool)}
                      />
                      <small className="text-xs text-text-tertiary">
                        {policy.isPending &&
                        policy.pendingToolId === tool.tool_id
                          ? "正在保存"
                          : "管理员审批策略"}
                      </small>
                    </div>
                    <Button
                      aria-label={`编辑业务展示：${tool.model_name}`}
                      disabled={controlsLocked}
                      onClick={() => setEditingToolId(tool.tool_id)}
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
                      编辑业务展示
                    </Button>
                  </div>
                </li>
              );
            })}
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

      {editingTool ? (
        <ToolMetadataDialog
          onClose={() => setEditingToolId(null)}
          open
          tool={editingTool}
        />
      ) : null}
    </div>
  );
}
