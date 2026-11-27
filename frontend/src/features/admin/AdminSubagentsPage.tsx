import { Pencil, Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { getAuthIdentityEpoch } from "../../shared/api/client";
import { formatDateTime } from "../../shared/lib/format";
import { parsePage } from "../../shared/lib/pagination";
import {
  ADMIN_CARD_ACTIONS_CLASS,
  ADMIN_CARD_CLASS,
  ADMIN_CARD_HEADING_CLASS,
  ADMIN_CARD_LIST_CLASS,
  ADMIN_DATA_COUNT_CLASS,
  ADMIN_DETAIL_GRID_CLASS,
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
import { Modal } from "../../shared/ui/Modal";
import { AdminPagination, lastPageNumber } from "./AdminPagination";
import { SubagentDialog } from "./SubagentDialog";
import { adminErrorMessage } from "./errorMessages";
import {
  useDeleteSubagentMutation,
  useModelProfiles,
  useSetSubagentEnabledMutation,
  useSubagents,
} from "./hooks";
import type { SubagentProfile } from "./model";

const PAGE_SIZE = 10;

export function AdminSubagentsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const page = parsePage(searchParams.get("page"));
  const subagents = useSubagents({ page, pageSize: PAGE_SIZE });
  const models = useModelProfiles();
  const toggleEnabled = useSetSubagentEnabledMutation();
  const deleteMutation = useDeleteSubagentMutation();

  const [createOpen, setCreateOpen] = useState(false);
  const [editSubagentId, setEditSubagentId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<SubagentProfile | null>(
    null,
  );
  const [actionError, setActionError] = useState<string | null>(null);

  const lastPage = subagents.isSuccess
    ? lastPageNumber(subagents.data.total, subagents.data.limit)
    : 1;

  useEffect(() => {
    if (subagents.isSuccess && page > lastPage) {
      setSearchParams(
        lastPage > 1 ? { page: String(lastPage) } : {},
        { replace: true },
      );
    }
  }, [lastPage, page, setSearchParams, subagents.isSuccess]);

  const goToPage = (nextPage: number) => {
    setSearchParams(
      nextPage > 1 ? { page: String(nextPage) } : {},
      { replace: true },
    );
  };

  const modelNameById = new Map(
    (models.data?.items ?? []).map((item) => [item.id, `${item.name} · ${item.model}`]),
  );

  const handleToggleEnabled = async (subagent: SubagentProfile) => {
    setActionError(null);
    try {
      await toggleEnabled.mutateAsync({
        subagentId: subagent.id,
        enabled: !subagent.enabled,
        identityEpoch: getAuthIdentityEpoch(),
      });
    } catch (error) {
      setActionError(adminErrorMessage(error));
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) {
      return;
    }
    setActionError(null);
    try {
      await deleteMutation.mutateAsync({
        subagentId: deleteTarget.id,
        identityEpoch: getAuthIdentityEpoch(),
      });
      setDeleteTarget(null);
    } catch (error) {
      setActionError(adminErrorMessage(error));
    }
  };

  return (
    <div className={cn(PAGE_SHELL_CLASS, "min-w-0")}>
      <header className={PAGE_HEADING_CLASS}>
        <div className={PAGE_HEADING_COPY_CLASS}>
          <h1 className={PAGE_TITLE_CLASS}>子智能体管理</h1>
          <p className={PAGE_DESC_CLASS}>
            配置可被主智能体异步派发的子智能体。每个子智能体拥有独立系统提示词、可选绑定模型与最小工具权限白名单。
          </p>
        </div>
        <Button
          onClick={() => setCreateOpen(true)}
          type="button"
          variant="primary"
        >
          <Plus aria-hidden="true" focusable="false" size={14} strokeWidth={1.7} />
          新建子智能体
        </Button>
      </header>

      {actionError ? (
        <InlineAlert size="compact" variant="danger">
          {actionError}
        </InlineAlert>
      ) : null}

      {subagents.isPending ? (
        <div className={PAGE_LOADING_CLASS} role="status">
          正在读取子智能体配置…
        </div>
      ) : subagents.isError ? (
        <InlineAlert
          action={
            <button
              className={TEXT_BUTTON_CLASS}
              onClick={() => void subagents.refetch()}
              type="button"
            >
              重试
            </button>
          }
          size="compact"
          variant="danger"
        >
          {adminErrorMessage(subagents.error)}
        </InlineAlert>
      ) : subagents.data.items.length === 0 && page === 1 ? (
        <EmptyState
          action={
            <Button
              onClick={() => setCreateOpen(true)}
              type="button"
              variant="primary"
            >
              新建第一个子智能体
            </Button>
          }
          description="创建并启用子智能体后，主智能体在对话中可通过 dispatch_subagents 并发派发专业子任务。"
          icon="spark"
          title="还没有子智能体"
        />
      ) : (
        <section
          aria-labelledby="subagent-list-title"
          className={ADMIN_SECTION_CLASS}
        >
          <header className={ADMIN_SECTION_HEADING_CLASS}>
            <div>
              <h2 id="subagent-list-title">子智能体目录</h2>
            </div>
            <span className={ADMIN_DATA_COUNT_CLASS}>
              共 {subagents.data.total} 个子智能体
            </span>
          </header>

          <ul className={ADMIN_CARD_LIST_CLASS}>
            {subagents.data.items.map((subagent) => {
              const toggling =
                toggleEnabled.isPending &&
                toggleEnabled.variables?.subagentId === subagent.id;
              const boundModelLabel = subagent.model_profile_id
                ? (modelNameById.get(subagent.model_profile_id) ??
                  subagent.model_profile_id)
                : "跟随系统默认";

              return (
                <li className={ADMIN_CARD_CLASS} key={subagent.id}>
                  <div className={ADMIN_CARD_HEADING_CLASS}>
                    <div className="min-w-0">
                      <div className={ADMIN_TITLE_LINE_CLASS}>
                        <h3>{subagent.name}</h3>
                        <code className="max-w-full rounded-md border border-border-default bg-subtle px-[7px] py-0.5 text-xs text-text-secondary [overflow-wrap:anywhere]">
                          {subagent.slug}
                        </code>
                        <Badge
                          className="rounded-full"
                          variant={subagent.enabled ? "brand" : "neutral"}
                        >
                          {subagent.enabled ? "已启用" : "已停用"}
                        </Badge>
                        <Badge className="rounded-full" variant="neutral">
                          v{subagent.version}
                        </Badge>
                        <VisibilityBadge
                          departmentName={
                            subagent.visibility_scope === "department"
                              ? subagent.owner_department_id
                              : null
                          }
                          scope={subagent.visibility_scope}
                        />
                      </div>
                      <p
                        className={cn(
                          ADMIN_MUTED_CLASS,
                          "mt-1.5 [overflow-wrap:anywhere]",
                        )}
                      >
                        {subagent.description}
                      </p>
                    </div>
                  </div>

                  <dl className={ADMIN_DETAIL_GRID_CLASS}>
                    <div>
                      <dt>绑定模型</dt>
                      <dd>{boundModelLabel}</dd>
                    </div>
                    <div>
                      <dt>已授权工具</dt>
                      <dd>{subagent.allowed_tool_ids.length} 个</dd>
                    </div>
                    <div>
                      <dt>最大请求 / 超时</dt>
                      <dd>
                        {subagent.max_llm_requests ?? "默认"} 次 ·{" "}
                        {subagent.timeout_seconds
                          ? `${subagent.timeout_seconds}s`
                          : "默认"}
                      </dd>
                    </div>
                    <div>
                      <dt>更新时间</dt>
                      <dd>{formatDateTime(subagent.updated_at)}</dd>
                    </div>
                  </dl>

                  {/* 操作统一放在卡片底部，避免长名称和元数据挤压按钮。 */}
                  <div
                    className={cn(
                      ADMIN_CARD_ACTIONS_CLASS,
                      "flex-wrap justify-end",
                    )}
                  >
                    <Button
                      aria-label={`${subagent.enabled ? "停用" : "启用"}子智能体：${subagent.name}`}
                      aria-pressed={subagent.enabled}
                      disabled={toggling}
                      onClick={() => void handleToggleEnabled(subagent)}
                      size="sm"
                      type="button"
                      variant="outline"
                    >
                      {toggling
                        ? "处理中…"
                        : subagent.enabled
                          ? "停用"
                          : "启用"}
                    </Button>
                    <Button
                      aria-label={`编辑子智能体：${subagent.name}`}
                      onClick={() => setEditSubagentId(subagent.id)}
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
                      aria-label={`删除子智能体：${subagent.name}`}
                      onClick={() => setDeleteTarget(subagent)}
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
                  </div>
                </li>
              );
            })}
          </ul>

          <AdminPagination
            label="子智能体分页"
            onPageChange={goToPage}
            page={page}
            pageSize={subagents.data.limit}
            total={subagents.data.total}
          />
        </section>
      )}

      <SubagentDialog
        onClose={() => setCreateOpen(false)}
        open={createOpen}
      />

      {editSubagentId ? (
        <SubagentDialog
          onClose={() => setEditSubagentId(null)}
          open
          subagentId={editSubagentId}
        />
      ) : null}

      <Modal
        closeDisabled={deleteMutation.isPending}
        description="删除后该子智能体将不可再被调度，历史运行记录不受影响。"
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
        title={`删除子智能体「${deleteTarget?.name ?? ""}」`}
      >
        <p>
          确认软删除子智能体 <code>{deleteTarget?.slug}</code> 吗？
        </p>
      </Modal>
    </div>
  );
}
