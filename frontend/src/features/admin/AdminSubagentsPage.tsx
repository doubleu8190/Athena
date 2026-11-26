import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { getAuthIdentityEpoch } from "../../shared/api/client";
import { formatDateTime } from "../../shared/lib/format";
import { VisibilityBadge } from "../../shared/ui";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
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

function parsePage(value: string | null): number {
  if (!value) {
    return 1;
  }
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 1;
}

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
    <div className="page-shell admin-page admin-subagents-page">
      <header className="page-heading">
        <div className="page-heading-copy">
          <h1>子智能体管理</h1>
          <p className="page-description">
            配置可被主智能体异步派发的子智能体。每个子智能体拥有独立系统提示词、可选绑定模型与最小工具权限白名单。
          </p>
        </div>
        <button
          className="button-primary"
          onClick={() => setCreateOpen(true)}
          type="button"
        >
          <Icon name="plus" size={14} />
          新建子智能体
        </button>
      </header>

      {actionError ? <ErrorBanner message={actionError} /> : null}

      {subagents.isPending ? (
        <div className="page-loading" role="status">
          正在读取子智能体配置…
        </div>
      ) : subagents.isError ? (
        <ErrorBanner
          message={adminErrorMessage(subagents.error)}
          onRetry={() => void subagents.refetch()}
        />
      ) : subagents.data.items.length === 0 && page === 1 ? (
        <EmptyState
          action={
            <button
              className="button-primary"
              onClick={() => setCreateOpen(true)}
              type="button"
            >
              新建第一个子智能体
            </button>
          }
          description="创建并启用子智能体后，主智能体在对话中可通过 dispatch_subagents 并发派发专业子任务。"
          icon="spark"
          title="还没有子智能体"
        />
      ) : (
        <section
          aria-labelledby="subagent-list-title"
          className="admin-section"
        >
          <header className="admin-section-heading">
            <div>
              <h2 id="subagent-list-title">子智能体目录</h2>
            </div>
            <span className="admin-data-count">
              共 {subagents.data.total} 个子智能体
            </span>
          </header>

          <ul className="admin-server-list">
            {subagents.data.items.map((subagent) => {
              const toggling =
                toggleEnabled.isPending &&
                toggleEnabled.variables?.subagentId === subagent.id;
              const boundModelLabel = subagent.model_profile_id
                ? (modelNameById.get(subagent.model_profile_id) ??
                  subagent.model_profile_id)
                : "跟随系统默认";

              return (
                <li className="admin-server-card admin-subagent-card" key={subagent.id}>
                  <div className="admin-card-heading">
                    <div>
                      <div className="admin-title-line">
                        <h3>{subagent.name}</h3>
                        <code className="admin-slug-badge">{subagent.slug}</code>
                        <span
                          className={`admin-status-badge${
                            subagent.enabled ? " is-accent" : ""
                          }`}
                        >
                          {subagent.enabled ? "已启用" : "已停用"}
                        </span>
                        <span className="admin-status-badge">
                          v{subagent.version}
                        </span>
                        <VisibilityBadge
                          departmentName={
                            subagent.visibility_scope === "department"
                              ? subagent.owner_department_id
                              : null
                          }
                          scope={subagent.visibility_scope}
                        />
                      </div>
                      <p className="admin-muted">{subagent.description}</p>
                    </div>
                  </div>

                  <dl className="admin-detail-grid">
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
                  <div className="admin-card-actions admin-subagent-card-actions">
                    <button
                      aria-label={`${subagent.enabled ? "停用" : "启用"}子智能体：${subagent.name}`}
                      aria-pressed={subagent.enabled}
                      className="button-quiet"
                      disabled={toggling}
                      onClick={() => void handleToggleEnabled(subagent)}
                      type="button"
                    >
                      {toggling
                        ? "处理中…"
                        : subagent.enabled
                          ? "停用"
                          : "启用"}
                    </button>
                    <button
                      aria-label={`编辑子智能体：${subagent.name}`}
                      className="button-quiet"
                      onClick={() => setEditSubagentId(subagent.id)}
                      type="button"
                    >
                      <Icon name="edit" size={14} />
                      编辑
                    </button>
                    <button
                      aria-label={`删除子智能体：${subagent.name}`}
                      className="button-danger-outline"
                      onClick={() => setDeleteTarget(subagent)}
                      type="button"
                    >
                      <Icon name="trash" size={14} />
                      删除
                    </button>
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
            <button
              className="button-quiet"
              disabled={deleteMutation.isPending}
              onClick={() => setDeleteTarget(null)}
              type="button"
            >
              取消
            </button>
            <button
              className="button-danger"
              disabled={deleteMutation.isPending}
              onClick={() => void handleDelete()}
              type="button"
            >
              {deleteMutation.isPending ? "正在删除…" : "确认删除"}
            </button>
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
