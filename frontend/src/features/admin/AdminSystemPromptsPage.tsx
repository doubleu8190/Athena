import { RefreshCw, ShieldCheck, Sparkle } from "lucide-react";

import { formatDateTime } from "../../shared/lib/format";
import { countCodePoints } from "../../shared/lib/text";
import {
  ADMIN_SECTION_CLASS,
  ADMIN_SECTION_HEADING_CLASS,
  Badge,
  Button,
  FIELD_CLASS,
  FIELD_TEXTAREA_CLASS,
  InlineAlert,
  NOTICE_WARNING_CLASS,
  PAGE_DESC_CLASS,
  PAGE_HEADING_CLASS,
  PAGE_HEADING_COPY_CLASS,
  PAGE_LOADING_CLASS,
  PAGE_SHELL_CLASS,
  PAGE_TITLE_CLASS,
  TEXT_BUTTON_CLASS,
  cn,
} from "../../shared/ui";
import { adminErrorMessage } from "./errorMessages";
import { SYSTEM_PROMPT_CONTENT_MAX_LENGTH } from "./model";
import {
  SystemPromptConfirmModal,
  SystemPromptHistoryPanel,
} from "./SystemPromptHistoryPanel";
import {
  PROMPT_KINDS,
  sourceLabel,
  useAdminSystemPrompts,
} from "./useAdminSystemPrompts";

/** 全局系统提示词管理页（主编辑区与顶部提示栏容器，状态与历史面板已拆解解耦） */
export function AdminSystemPromptsPage() {
  const {
    actionPending,
    activeDefinition,
    activeKind,
    cancelPendingConfirmation,
    confirmDiscard,
    confirmationCopy,
    copyStatus,
    current,
    editor,
    handleCopy,
    handleDraftChange,
    handleHistoryPageChange,
    handleReloadCurrent,
    handleSave,
    handleVerifyRestore,
    history,
    historyPage,
    isDirty,
    latestServerVersionChanged,
    modalActionPending,
    notice,
    openReloadConfirmation,
    openRestoreConfirmation,
    pendingConfirmation,
    requestKindChange,
    routeSaveAttempt,
    saving,
    selectedVersion,
    selectedVersionId,
    toggleSelectedVersion,
    validationError,
    verificationPending,
  } = useAdminSystemPrompts();

  return (
    <div className={cn(PAGE_SHELL_CLASS, "min-w-0")}>
      <header className={PAGE_HEADING_CLASS}>
        <div className={PAGE_HEADING_COPY_CLASS}>
          <h1 className={PAGE_TITLE_CLASS}>系统提示词</h1>
          <p className={PAGE_DESC_CLASS}>
            管理主对话与历史压缩的全局纯文本提示词。保存会创建不可变版本并立即供后续新任务使用，已有任务继续使用创建时固定的版本。
          </p>
        </div>
      </header>

      <div
        aria-label="系统提示词类别"
        className="mb-4 grid grid-cols-1 gap-[9px] sm:grid-cols-2"
        role="tablist"
      >
        {PROMPT_KINDS.map((definition) => {
          const isActive = activeKind === definition.kind;
          return (
            <button
              aria-controls={`system-prompt-panel-${definition.kind}`}
              aria-selected={isActive}
              className={cn(
                "flex min-w-0 flex-col items-start gap-1 rounded-[11px] border px-4 py-3.5 text-left transition-colors disabled:cursor-wait disabled:opacity-70",
                isActive
                  ? "border-brand-border bg-brand-subtle text-brand-hover shadow-[inset_3px_0_0_var(--brand-hover)]"
                  : "border-border-default bg-canvas text-text-secondary hover:enabled:border-brand-border hover:enabled:bg-subtle",
              )}
              disabled={actionPending}
              id={`system-prompt-tab-${definition.kind}`}
              key={definition.kind}
              onClick={() => requestKindChange(definition.kind)}
              role="tab"
              type="button"
            >
              <strong
                className={cn(
                  "text-base font-semibold",
                  isActive ? "text-brand" : "text-text-primary",
                )}
              >
                {definition.label}
              </strong>
              <span className="text-xs leading-[1.55]">
                {definition.description}
              </span>
            </button>
          );
        })}
      </div>

      {notice ? (
        <div
          className={cn(
            "mb-4 flex items-start gap-2 rounded-[9px] border px-3 py-[11px] text-xs leading-[1.65] [&>svg]:mt-0.5 [&>svg]:shrink-0",
            notice.kind === "success"
              ? "border-success-border bg-success-bg text-success"
              : "border-danger-border bg-danger-bg text-danger",
          )}
          role={notice.kind === "error" ? "alert" : "status"}
        >
          {notice.kind === "error" ? (
            <ShieldCheck
              aria-hidden="true"
              focusable="false"
              size={16}
              strokeWidth={1.7}
            />
          ) : (
            <Sparkle
              aria-hidden="true"
              focusable="false"
              size={16}
              strokeWidth={1.7}
            />
          )}
          <div className="flex min-w-0 flex-1 flex-col items-start gap-2">
            <span className="[overflow-wrap:anywhere]">{notice.text}</span>
            {notice.action === "verify-restore" ? (
              <Button
                disabled={actionPending}
                onClick={() => void handleVerifyRestore()}
                size="sm"
                type="button"
                variant="outline"
              >
                <RefreshCw
                  aria-hidden="true"
                  focusable="false"
                  size={14}
                  strokeWidth={1.7}
                />
                {verificationPending
                  ? "正在重新读取…"
                  : "重新读取当前版本与历史"}
              </Button>
            ) : null}
          </div>
        </div>
      ) : null}

      <div
        aria-labelledby={`system-prompt-tab-${activeKind}`}
        id={`system-prompt-panel-${activeKind}`}
        role="tabpanel"
      >
        {current.isPending ? (
          <div className={PAGE_LOADING_CLASS} role="status">
            正在读取{activeDefinition?.label}系统提示词…
          </div>
        ) : current.isError && !current.data ? (
          <InlineAlert
            action={
              <button
                className={TEXT_BUTTON_CLASS}
                onClick={() => void current.refetch()}
                type="button"
              >
                重新读取
              </button>
            }
            size="compact"
            variant="danger"
          >
            {adminErrorMessage(current.error)}
          </InlineAlert>
        ) : editor ? (
          <div className="grid min-w-0 grid-cols-1 items-start gap-[18px] lg:grid-cols-[minmax(0,1.45fr)_minmax(300px,0.8fr)]">
            <section
              aria-labelledby="system-prompt-editor-title"
              className={ADMIN_SECTION_CLASS}
            >
              <header className={ADMIN_SECTION_HEADING_CLASS}>
                <div>
                  <h2 id="system-prompt-editor-title">
                    {activeDefinition?.label}正文
                  </h2>
                </div>
                <Badge className="rounded-full" variant="brand">
                  {latestServerVersionChanged ? "编辑基线" : "当前版本"}{" "}
                  {editor.baselineId}
                </Badge>
              </header>

              {current.isError ? (
                <div className="mb-4">
                  <InlineAlert
                    action={
                      <button
                        className={TEXT_BUTTON_CLASS}
                        onClick={() => void current.refetch()}
                        type="button"
                      >
                        重新读取
                      </button>
                    }
                    size="compact"
                    variant="danger"
                  >
                    {`后台复核当前版本失败。${adminErrorMessage(current.error)}`}
                  </InlineAlert>
                </div>
              ) : null}

              <dl className="mb-3.5 grid grid-cols-1 gap-[9px] sm:grid-cols-3 [&>div]:min-w-0 [&>div]:rounded-[9px] [&>div]:border [&>div]:border-border-default [&>div]:bg-subtle [&>div]:px-[11px] [&>div]:py-2.5 [&_dt]:text-xs [&_dt]:text-text-tertiary [&_dd]:mt-[3px] [&_dd]:text-xs [&_dd]:leading-[1.55] [&_dd]:font-semibold [&_dd]:text-text-primary [&_dd]:[overflow-wrap:anywhere]">
                <div>
                  <dt>修改人</dt>
                  <dd>{sourceLabel(editor.baselineCreatedBy)}</dd>
                </div>
                <div>
                  <dt>修改时间</dt>
                  <dd>{formatDateTime(editor.baselineCreatedAt)}</dd>
                </div>
                <div>
                  <dt>版本来源</dt>
                  <dd>
                    {editor.baselineId === current.data?.id
                      ? "当前生效版本"
                      : "加载时的生效版本"}
                  </dd>
                </div>
              </dl>

              <label className={FIELD_CLASS} htmlFor="system-prompt-content">
                <span>完整正文（纯文本）</span>
                <textarea
                  aria-label="完整正文（纯文本）"
                  aria-describedby="system-prompt-content-help"
                  className={cn(
                    FIELD_TEXTAREA_CLASS,
                    "min-h-[clamp(360px,52vh,680px)] font-mono text-[13px] leading-[1.7] whitespace-pre-wrap",
                  )}
                  disabled={actionPending}
                  id="system-prompt-content"
                  onChange={(event) => handleDraftChange(event.target.value)}
                  spellCheck={false}
                  value={editor.draft}
                />
                <small id="system-prompt-content-help">
                  最多 {SYSTEM_PROMPT_CONTENT_MAX_LENGTH.toLocaleString()} 个字符，换行和首尾空白会按原样保存。
                </small>
              </label>

              <div className="mt-[7px] flex items-center justify-between gap-3.5 text-xs text-text-tertiary">
                <span>
                  {countCodePoints(editor.draft).toLocaleString()} /{" "}
                  {SYSTEM_PROMPT_CONTENT_MAX_LENGTH.toLocaleString()} 字符
                </span>
                <span>
                  {isDirty ? "有未保存修改" : "与加载版本一致"}
                </span>
              </div>

              {isDirty && validationError ? (
                <p
                  className="mt-[9px] text-xs font-semibold text-danger"
                  role="alert"
                >
                  {validationError}
                </p>
              ) : null}

              {latestServerVersionChanged && isDirty ? (
                <div
                  className={cn(
                    NOTICE_WARNING_CLASS,
                    "mt-3 flex flex-col items-stretch justify-between gap-[15px] rounded-[9px] p-3 sm:flex-row sm:items-start",
                  )}
                  role="alert"
                >
                  <div>
                    <strong className="text-xs">
                      服务端当前版本已在编辑期间变化
                    </strong>
                    <p className="mt-1 text-xs leading-[1.65]">
                      服务端当前版本为 {current.data?.id}；本地输入和上方
                      {editor.baselineId} 基线均未改动。保存将触发 CAS
                      冲突，请先复制需要保留的内容，再重新载入服务端版本核对。
                    </p>
                  </div>
                  <div className="flex shrink-0 flex-wrap justify-stretch gap-1.5 sm:justify-end max-sm:[&>button]:flex-1">
                    <Button
                      disabled={actionPending}
                      onClick={() => void handleCopy()}
                      size="sm"
                      type="button"
                      variant="outline"
                    >
                      复制当前输入
                    </Button>
                    <Button
                      disabled={actionPending}
                      onClick={openReloadConfirmation}
                      size="sm"
                      type="button"
                      variant="outline"
                    >
                      <RefreshCw
                        aria-hidden="true"
                        focusable="false"
                        size={14}
                        strokeWidth={1.7}
                      />
                      重新加载服务端版本
                    </Button>
                  </div>
                </div>
              ) : null}

              {copyStatus ? (
                <p className="mt-2 text-xs text-brand-hover" role="status">
                  {copyStatus}
                </p>
              ) : null}

              <footer className="mt-[15px] flex flex-col items-stretch justify-between gap-3.5 border-t border-border-subtle pt-[13px] sm:flex-row sm:items-center">
                <p className="max-w-[560px] text-xs leading-[1.65] text-text-tertiary">
                  保存后立即创建不可变版本。并发修改会返回冲突，当前输入不会被覆盖。
                </p>
                <div className="flex shrink-0 gap-[7px] max-sm:[&>button]:flex-1">
                  {isDirty ? (
                    <Button
                      disabled={actionPending}
                      onClick={openReloadConfirmation}
                      type="button"
                      variant="outline"
                    >
                      放弃修改并重新加载
                    </Button>
                  ) : null}
                  <Button
                    disabled={
                      !isDirty || Boolean(validationError) || actionPending
                    }
                    onClick={() => void handleSave()}
                    type="button"
                    variant="primary"
                  >
                    {saving ? "正在保存…" : "保存并生效"}
                  </Button>
                </div>
              </footer>
            </section>

            <SystemPromptHistoryPanel
              actionPending={actionPending}
              currentVersionId={current.data?.id}
              history={history}
              historyPage={historyPage}
              onPageChange={handleHistoryPageChange}
              onRequestRestore={openRestoreConfirmation}
              onToggleVersion={toggleSelectedVersion}
              selectedVersion={selectedVersion}
              selectedVersionId={selectedVersionId}
            />
          </div>
        ) : null}
      </div>

      <SystemPromptConfirmModal
        confirmationCopy={confirmationCopy}
        copyStatus={copyStatus}
        draftText={editor?.draft ?? ""}
        modalActionPending={modalActionPending}
        onCancel={cancelPendingConfirmation}
        onConfirmDiscard={confirmDiscard}
        onConfirmReload={() => void handleReloadCurrent()}
        onCopy={() => void handleCopy()}
        pendingConfirmation={pendingConfirmation}
        routeSaveAttempt={routeSaveAttempt}
      />
    </div>
  );
}
