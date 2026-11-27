import { Eye, History } from "lucide-react";

import { formatDateTime } from "../../shared/lib/format";
import { countCodePoints } from "../../shared/lib/text";
import {
  ADMIN_CODE_VALUE_CLASS,
  ADMIN_DATA_COUNT_CLASS,
  ADMIN_SECTION_CLASS,
  ADMIN_SECTION_HEADING_CLASS,
  ADMIN_TITLE_LINE_CLASS,
  Badge,
  Button,
  EMPTY_INLINE_CLASS,
  InlineAlert,
  MODAL_WIDTH_MD,
  PAGE_LOADING_CLASS,
  TEXT_BUTTON_CLASS,
  cn,
} from "../../shared/ui";
import { Modal } from "../../shared/ui/Modal";
import { AdminPagination } from "./AdminPagination";
import { adminErrorMessage } from "./errorMessages";
import type {
  SystemPromptVersion,
  SystemPromptVersionListResponse,
  SystemPromptVersionSummary,
} from "./model";
import {
  sourceLabel,
  type ConfirmationCopy,
  type PendingConfirmation,
  type SaveAttempt,
} from "./useAdminSystemPrompts";

interface HistoryQueryLike {
  data?: SystemPromptVersionListResponse;
  error: unknown;
  isError: boolean;
  isPending: boolean;
  isSuccess: boolean;
  refetch: () => Promise<unknown>;
}

interface SelectedVersionQueryLike {
  data?: SystemPromptVersion;
  error: unknown;
  isError: boolean;
  isPending: boolean;
  refetch: () => Promise<unknown>;
}

interface SystemPromptHistoryPanelProps {
  actionPending: boolean;
  currentVersionId?: string;
  history: HistoryQueryLike;
  historyPage: number;
  onPageChange: (nextPage: number) => void;
  onRequestRestore: (source: SystemPromptVersion) => void;
  onToggleVersion: (versionId: string) => void;
  selectedVersion: SelectedVersionQueryLike;
  selectedVersionId: string | null;
}

/** 系统提示词不可变版本历史分页列表与全文预览面板 */
export function SystemPromptHistoryPanel({
  actionPending,
  currentVersionId,
  history,
  historyPage,
  onPageChange,
  onRequestRestore,
  onToggleVersion,
  selectedVersion,
  selectedVersionId,
}: SystemPromptHistoryPanelProps) {
  return (
    <section
      aria-labelledby="system-prompt-history-title"
      className={ADMIN_SECTION_CLASS}
    >
      <header className={ADMIN_SECTION_HEADING_CLASS}>
        <div>
          <h2 id="system-prompt-history-title">版本历史</h2>
        </div>
        {history.isSuccess && history.data ? (
          <span className={ADMIN_DATA_COUNT_CLASS}>
            共 {history.data.total} 个版本
          </span>
        ) : null}
      </header>

      {history.isPending ? (
        <div className={PAGE_LOADING_CLASS} role="status">
          正在读取版本历史…
        </div>
      ) : history.isError ? (
        <InlineAlert
          action={
            <button
              className={TEXT_BUTTON_CLASS}
              onClick={() => void history.refetch()}
              type="button"
            >
              重新读取历史
            </button>
          }
          size="compact"
          variant="danger"
        >
          {adminErrorMessage(history.error)}
        </InlineAlert>
      ) : !history.data || history.data.items.length === 0 ? (
        <p className={EMPTY_INLINE_CLASS}>后端尚未返回任何历史版本。</p>
      ) : (
        <>
          <ol className="m-0 flex list-none flex-col gap-2 p-0">
            {history.data.items.map((version: SystemPromptVersionSummary) => {
              const isCurrent = version.id === currentVersionId;
              return (
                <li
                  className={cn(
                    "flex min-w-0 flex-col items-start justify-between gap-2.5 rounded-[9px] border bg-canvas p-[11px] sm:flex-row",
                    isCurrent
                      ? "border-brand-border shadow-[inset_3px_0_0_var(--brand-hover)]"
                      : "border-border-default",
                  )}
                  key={version.id}
                >
                  <div className="min-w-0">
                    <div className={ADMIN_TITLE_LINE_CLASS}>
                      <strong className={ADMIN_CODE_VALUE_CLASS}>
                        {version.id}
                      </strong>
                      {isCurrent ? (
                        <Badge className="rounded-full" variant="success">
                          当前生效
                        </Badge>
                      ) : null}
                      {version.restored_from_version_id ? (
                        <Badge className="rounded-full" variant="neutral">
                          恢复自 {version.restored_from_version_id}
                        </Badge>
                      ) : null}
                    </div>
                    <p className="mt-[5px] text-xs leading-[1.55] text-text-tertiary [overflow-wrap:anywhere]">
                      {sourceLabel(version.created_by)} ·{" "}
                      {formatDateTime(version.created_at)}
                    </p>
                  </div>
                  <Button
                    aria-label={`查看版本 ${version.id}`}
                    className="shrink-0"
                    disabled={actionPending}
                    onClick={() => onToggleVersion(version.id)}
                    size="sm"
                    type="button"
                    variant="outline"
                  >
                    <Eye
                      aria-hidden="true"
                      focusable="false"
                      size={14}
                      strokeWidth={1.7}
                    />
                    {selectedVersionId === version.id
                      ? "收起正文"
                      : "查看正文"}
                  </Button>
                </li>
              );
            })}
          </ol>

          {selectedVersionId ? (
            <div className="mt-3 overflow-hidden rounded-[10px] border border-border-default bg-subtle">
              {selectedVersion.isPending ? (
                <div className={PAGE_LOADING_CLASS} role="status">
                  正在读取历史正文…
                </div>
              ) : selectedVersion.isError ? (
                <InlineAlert
                  action={
                    <button
                      className={TEXT_BUTTON_CLASS}
                      onClick={() => void selectedVersion.refetch()}
                      type="button"
                    >
                      重新读取正文
                    </button>
                  }
                  size="compact"
                  variant="danger"
                >
                  {adminErrorMessage(selectedVersion.error)}
                </InlineAlert>
              ) : selectedVersion.data ? (
                <>
                  <header className="flex flex-col items-start justify-between gap-3 border-b border-border-subtle px-3 py-[11px] sm:flex-row sm:items-center">
                    <div className="flex min-w-0 flex-col gap-0.5">
                      <span className="text-xs text-text-tertiary">
                        历史版本完整正文
                      </span>
                      <strong className="font-mono text-xs text-brand [overflow-wrap:anywhere]">
                        {selectedVersion.data.id}
                      </strong>
                    </div>
                    <Button
                      disabled={
                        actionPending ||
                        selectedVersion.data.id === currentVersionId
                      }
                      onClick={() => {
                        if (selectedVersion.data) {
                          onRequestRestore(selectedVersion.data);
                        }
                      }}
                      size="sm"
                      type="button"
                      variant="outline"
                    >
                      <History
                        aria-hidden="true"
                        focusable="false"
                        size={14}
                        strokeWidth={1.7}
                      />
                      {selectedVersion.data.id === currentVersionId
                        ? "当前生效版本"
                        : "恢复此版本"}
                    </Button>
                  </header>
                  <pre className="m-0 max-h-[460px] overflow-auto p-3.5 font-mono text-xs leading-[1.7] break-words whitespace-pre-wrap text-text-primary">
                    {selectedVersion.data.content}
                  </pre>
                </>
              ) : null}
            </div>
          ) : null}

          <AdminPagination
            label="系统提示词版本分页"
            onPageChange={onPageChange}
            page={historyPage}
            pageSize={history.data.page_size}
            total={history.data.total}
          />
        </>
      )}
    </section>
  );
}

interface SystemPromptConfirmModalProps {
  confirmationCopy: ConfirmationCopy | null;
  copyStatus: string | null;
  draftText: string;
  modalActionPending: boolean;
  onCancel: () => void;
  onConfirmDiscard: () => void;
  onConfirmReload: () => void;
  onCopy: () => void;
  pendingConfirmation: PendingConfirmation | null;
  routeSaveAttempt: SaveAttempt | null;
}

/** 系统提示词切换/离页/重载/历史恢复二次确认弹窗 */
export function SystemPromptConfirmModal({
  confirmationCopy,
  copyStatus,
  draftText,
  modalActionPending,
  onCancel,
  onConfirmDiscard,
  onConfirmReload,
  onCopy,
  pendingConfirmation,
  routeSaveAttempt,
}: SystemPromptConfirmModalProps) {
  return (
    <Modal
      closeDisabled={modalActionPending}
      description={confirmationCopy?.description}
      footer={
        <div className="flex flex-wrap justify-end gap-[7px] max-sm:[&>button]:flex-1">
          <Button
            disabled={modalActionPending}
            onClick={onCancel}
            type="button"
            variant="outline"
          >
            {routeSaveAttempt?.status === "pending" &&
            pendingConfirmation?.type === "route"
              ? "留在此页"
              : "继续编辑"}
          </Button>
          <Button
            disabled={modalActionPending}
            onClick={onCopy}
            type="button"
            variant="outline"
          >
            复制当前输入
          </Button>
          <Button
            disabled={modalActionPending}
            onClick={
              pendingConfirmation?.type === "reload"
                ? onConfirmReload
                : onConfirmDiscard
            }
            type="button"
            variant="primary"
          >
            {modalActionPending
              ? "正在处理…"
              : confirmationCopy?.confirmLabel}
          </Button>
        </div>
      }
      onClose={() => {
        if (!modalActionPending) {
          onCancel();
        }
      }}
      open={Boolean(pendingConfirmation)}
      panelClassName={MODAL_WIDTH_MD}
      title={confirmationCopy?.title ?? ""}
    >
      <p className="text-xs leading-[1.75] text-text-secondary">
        当前正文 {countCodePoints(draftText)} 个字符。
        {copyStatus ? ` ${copyStatus}` : ""}
      </p>
    </Modal>
  );
}
