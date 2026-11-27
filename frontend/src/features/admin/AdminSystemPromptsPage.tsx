import { useCallback, useEffect, useRef, useState } from "react";
import { useBlocker } from "react-router-dom";

import {
  ApiError,
  getAuthIdentityEpoch,
  isAuthIdentityCurrent,
} from "../../shared/api/client";
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
  FIELD_CLASS,
  FIELD_TEXTAREA_CLASS,
  MODAL_WIDTH_MD,
  NOTICE_WARNING_CLASS,
  PAGE_DESC_CLASS,
  PAGE_HEADING_CLASS,
  PAGE_HEADING_COPY_CLASS,
  PAGE_LOADING_CLASS,
  PAGE_SHELL_CLASS,
  PAGE_TITLE_CLASS,
  cn,
} from "../../shared/ui";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { Modal } from "../../shared/ui/Modal";
import { useAuth } from "../auth/AuthProvider";
import { isSuperAdmin } from "../auth/model";
import {
  AdminPagination,
  lastPageNumber,
} from "./AdminPagination";
import { adminErrorMessage } from "./errorMessages";
import {
  useRestoreSystemPromptMutation,
  useSaveSystemPromptMutation,
  useSystemPrompt,
  useSystemPromptVersion,
  useSystemPromptVersions,
} from "./hooks";
import {
  SYSTEM_PROMPT_CONTENT_MAX_LENGTH,
  validateSystemPromptContent,
  type SystemPromptKind,
  type SystemPromptVersion,
} from "./model";

const HISTORY_PAGE_SIZE = 10;

const PROMPT_KINDS: Array<{
  description: string;
  kind: SystemPromptKind;
  label: string;
}> = [
  {
    description: "用于用户对话、工具调用后续轮次、澄清与审批恢复。",
    kind: "main",
    label: "主对话",
  },
  {
    description: "用于把较早对话压缩为可继续使用的历史摘要。",
    kind: "compression",
    label: "历史压缩",
  },
];

interface EditorState {
  baselineContent: string;
  baselineCreatedAt: string;
  baselineCreatedBy: string | null;
  baselineId: string;
  draft: string;
  kind: SystemPromptKind;
}

type PendingConfirmation =
  | {
      kind: SystemPromptKind;
      type: "switch-kind";
    }
  | {
      type: "route";
    }
  | {
      type: "reload";
    }
  | {
      source: SystemPromptVersion;
      type: "restore";
    };

interface Notice {
  action?: "verify-restore";
  kind: "error" | "success";
  text: string;
}

interface SaveAttemptBase {
  id: number;
  submittedContent: string;
}

type SaveAttempt =
  | (SaveAttemptBase & { status: "pending" })
  | (SaveAttemptBase & { status: "rejected" })
  | (SaveAttemptBase & { status: "uncertain" })
  | (SaveAttemptBase & {
      status: "success";
      version: SystemPromptVersion;
    });

function editorFromVersion(version: SystemPromptVersion): EditorState {
  return {
    baselineContent: version.content,
    baselineCreatedAt: version.created_at,
    baselineCreatedBy: version.created_by,
    baselineId: version.id,
    draft: version.content,
    kind: version.kind,
  };
}

function sourceLabel(createdBy: string | null): string {
  return createdBy?.trim() || "系统初始化";
}

function isUncertainWriteError(error: unknown): boolean {
  return (
    !(error instanceof ApiError) ||
    error.status === 0 ||
    error.status >= 500 ||
    error.detail === "invalid_response"
  );
}

async function copyText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }

  const fallback = document.createElement("textarea");
  fallback.value = value;
  fallback.setAttribute("readonly", "");
  fallback.style.position = "fixed";
  fallback.style.opacity = "0";
  document.body.appendChild(fallback);
  fallback.select();
  const copied = document.execCommand?.("copy") ?? false;
  fallback.remove();
  if (!copied) {
    throw new Error("clipboard_unavailable");
  }
}

export function AdminSystemPromptsPage() {
  const { status, superAdminAccessDenied, user } = useAuth();
  const [activeKind, setActiveKind] = useState<SystemPromptKind>("main");
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [historyPage, setHistoryPage] = useState(1);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(
    null,
  );
  const [notice, setNotice] = useState<Notice | null>(null);
  const [pendingConfirmation, setPendingConfirmation] =
    useState<PendingConfirmation | null>(null);
  const [copyStatus, setCopyStatus] = useState<string | null>(null);
  const [reloadPending, setReloadPending] = useState(false);
  const [verificationPending, setVerificationPending] = useState(false);
  const [saveAttempt, setSaveAttempt] = useState<SaveAttempt | null>(null);
  const [routeSaveAttemptId, setRouteSaveAttemptId] = useState<
    number | null
  >(null);
  const nextSaveAttemptId = useRef(0);
  const current = useSystemPrompt(activeKind);
  const history = useSystemPromptVersions(activeKind, {
    page: historyPage,
    pageSize: HISTORY_PAGE_SIZE,
  });
  const selectedVersion = useSystemPromptVersion(
    activeKind,
    selectedVersionId,
  );
  const saveMutation = useSaveSystemPromptMutation(activeKind);
  const restoreMutation = useRestoreSystemPromptMutation(activeKind);

  const isDirty =
    editor?.kind === activeKind &&
    editor.draft !== editor.baselineContent;
  const shouldBlockNavigation = useCallback(
    ({
      currentLocation,
      nextLocation,
    }: {
      currentLocation: { hash: string; pathname: string; search: string };
      nextLocation: { hash: string; pathname: string; search: string };
    }) =>
      status === "authenticated" &&
      !superAdminAccessDenied &&
      isSuperAdmin(user) &&
      Boolean(isDirty) &&
      (currentLocation.pathname !== nextLocation.pathname ||
        currentLocation.search !== nextLocation.search ||
        currentLocation.hash !== nextLocation.hash),
    [isDirty, status, superAdminAccessDenied, user],
  );
  const blocker = useBlocker(shouldBlockNavigation);
  const validationError = editor
    ? validateSystemPromptContent(editor.draft)
    : null;
  const latestServerVersionChanged =
    Boolean(editor && current.data) &&
    editor?.baselineId !== current.data?.id;
  const actionPending =
    saveMutation.isPending ||
    restoreMutation.isPending ||
    reloadPending ||
    verificationPending;
  const routeSaveAttempt =
    routeSaveAttemptId !== null && saveAttempt?.id === routeSaveAttemptId
      ? saveAttempt
      : null;
  // 用提交时正文快照区分“旧请求结果未知”与“之后新增但从未提交的草稿”。
  const routeHasLaterEdits =
    Boolean(routeSaveAttempt && editor) &&
    editor?.draft !== routeSaveAttempt?.submittedContent;
  const activeDefinition = PROMPT_KINDS.find(
    (definition) => definition.kind === activeKind,
  );
  const lastPage = history.isSuccess
    ? lastPageNumber(history.data.total, history.data.page_size)
    : 1;

  useEffect(() => {
    if (!current.data) {
      return;
    }
    setEditor((previous) => {
      if (!previous || previous.kind !== activeKind) {
        return editorFromVersion(current.data);
      }
      if (previous.baselineId === current.data.id) {
        return previous;
      }
      if (previous.draft === previous.baselineContent) {
        // 本地没有修改时自动跟随服务端当前版本，不制造假冲突。
        return editorFromVersion(current.data);
      }
      // 有本地输入时继续以加载时版本作为 CAS 基线，服务端更新只提示不覆盖。
      return previous;
    });
  }, [activeKind, current.data]);

  useEffect(() => {
    if (history.isSuccess && historyPage > lastPage) {
      setHistoryPage(lastPage);
    }
  }, [history.isSuccess, historyPage, lastPage]);

  useEffect(() => {
    if (!isDirty) {
      return;
    }
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      // 浏览器原生离开提示覆盖关闭标签页、刷新和跨站跳转。
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [isDirty]);

  useEffect(() => {
    if (blocker.state !== "blocked") {
      return;
    }
    // data-router 已暂停真实导航，确认 UI 只负责 reset 或 proceed。
    setCopyStatus(null);
    // 锁定打开弹窗时的保存尝试，后续请求完成不能把语义退回“未提交”。
    setRouteSaveAttemptId(saveAttempt?.id ?? null);
    setPendingConfirmation({ type: "route" });
  }, [blocker.state, saveAttempt?.id]);

  useEffect(() => {
    if (
      pendingConfirmation?.type === "route" &&
      blocker.state !== "blocked"
    ) {
      // 路由层若已自行解除 blocked，关闭陈旧确认，避免继续遮挡成功后的导航。
      setPendingConfirmation(null);
      setRouteSaveAttemptId(null);
      setCopyStatus(null);
    }
  }, [blocker.state, pendingConfirmation?.type]);

  const resetEditorState = () => {
    setEditor(null);
    setSelectedVersionId(null);
    setSaveAttempt(null);
    setRouteSaveAttemptId(null);
    setCopyStatus(null);
    setNotice(null);
  };

  const switchKind = (kind: SystemPromptKind) => {
    setActiveKind(kind);
    setHistoryPage(1);
    resetEditorState();
  };

  const requestKindChange = (kind: SystemPromptKind) => {
    if (kind === activeKind || actionPending) {
      return;
    }
    if (isDirty) {
      setCopyStatus(null);
      setPendingConfirmation({ kind, type: "switch-kind" });
      return;
    }
    switchKind(kind);
  };

  const applyCurrentVersion = (
    version: SystemPromptVersion,
    message: string,
  ) => {
    // 保存、恢复和显式刷新成功后才能替换本地基线；失败路径始终保留草稿。
    setEditor(editorFromVersion(version));
    setSelectedVersionId(null);
    setNotice({ kind: "success", text: message });
  };

  const handleSave = async () => {
    if (!editor || !isDirty || validationError || actionPending) {
      return;
    }
    const identityEpoch = getAuthIdentityEpoch();
    const attemptId = ++nextSaveAttemptId.current;
    const submittedContent = editor.draft;
    // 保存一发出就记录独立 attempt，离开弹窗随后绑定该记录而不是实时 pending。
    setSaveAttempt({
      id: attemptId,
      status: "pending",
      submittedContent,
    });
    setNotice(null);
    try {
      const version = await saveMutation.mutateAsync({
        content: submittedContent,
        expectedCurrentVersionId: editor.baselineId,
        identityEpoch,
      });
      if (!isAuthIdentityCurrent(identityEpoch)) {
        return;
      }
      setSaveAttempt({
        id: attemptId,
        status: "success",
        submittedContent,
        version,
      });
      const baselineId = editor.baselineId;
      applyCurrentVersion(
        version,
        version.id === baselineId
          ? "正文与当前版本相同，后端未创建重复版本。"
          : "已保存，新任务使用新版本。旧任务继续使用创建时固定的版本。",
      );
    } catch (error) {
      if (!isAuthIdentityCurrent(identityEpoch)) {
        return;
      }
      setSaveAttempt({
        id: attemptId,
        status: isUncertainWriteError(error) ? "uncertain" : "rejected",
        submittedContent,
      });
      setNotice({
        kind: "error",
        text: `${adminErrorMessage(error)} 当前输入仍保留。请重新读取当前版本核实是否已生效，系统不会自动重试。`,
      });
    }
  };

  const handleReloadCurrent = async () => {
    setReloadPending(true);
    setCopyStatus(null);
    try {
      const result = await current.refetch({ throwOnError: true });
      if (!result.data) {
        throw new Error("system_prompt_unavailable");
      }
      await history.refetch();
      applyCurrentVersion(
        result.data,
        "已重新载入服务端当前版本，本地编辑基线已更新。",
      );
      // 服务端正文已重新核实，旧保存结果与旧路由确认不能再影响后续编辑。
      setSaveAttempt(null);
      setRouteSaveAttemptId(null);
      setPendingConfirmation(null);
    } catch (error) {
      setPendingConfirmation(null);
      setNotice({
        kind: "error",
        text: `${adminErrorMessage(error)} 未能确认服务端版本，本地输入仍保留。`,
      });
    } finally {
      setReloadPending(false);
    }
  };

  const handleRestore = async (source: SystemPromptVersion) => {
    if (!editor || actionPending) {
      return;
    }
    const identityEpoch = getAuthIdentityEpoch();
    setNotice(null);
    try {
      const version = await restoreMutation.mutateAsync({
        expectedCurrentVersionId: editor.baselineId,
        identityEpoch,
        sourceVersionId: source.id,
      });
      if (!isAuthIdentityCurrent(identityEpoch)) {
        return;
      }
      setPendingConfirmation(null);
      setSaveAttempt(null);
      setRouteSaveAttemptId(null);
      applyCurrentVersion(
        version,
        "已恢复。旧内容已生成新版本并立即对后续新任务生效。",
      );
    } catch (error) {
      if (!isAuthIdentityCurrent(identityEpoch)) {
        return;
      }
      // 网络中断、5xx 和无法解析的响应不能证明恢复未提交；明确 4xx 才按未生效处理。
      const uncertain = isUncertainWriteError(error);
      setPendingConfirmation(null);
      setNotice({
        action: "verify-restore",
        kind: "error",
        text: uncertain
          ? `${adminErrorMessage(error)} 本次恢复（来源版本 ${source.id}）结果不确定，可能已经生效。当前输入与版本历史仍保留；请重新读取当前版本与版本历史核实，系统不会自动重复恢复。`
          : `${adminErrorMessage(error)} 本次恢复（来源版本 ${source.id}）未生效。当前输入与版本历史仍保留；可重新读取当前版本与版本历史核对，系统不会自动重复恢复。`,
      });
    }
  };

  const handleVerifyRestore = async () => {
    if (actionPending) {
      return;
    }
    const identityEpoch = getAuthIdentityEpoch();
    setVerificationPending(true);
    setCopyStatus(null);
    try {
      const [currentResult, historyResult] = await Promise.all([
        current.refetch({ throwOnError: true }),
        history.refetch({ throwOnError: true }),
      ]);
      if (!isAuthIdentityCurrent(identityEpoch)) {
        return;
      }
      if (!currentResult.data || !historyResult.data) {
        throw new Error("system_prompt_unavailable");
      }
      // 只刷新核对数据，不替换本地草稿、编辑基线或已展开的历史版本。
      setNotice({
        kind: "success",
        text: "已重新读取服务端当前版本与版本历史。请核对本次恢复是否生成新版本；本地输入未被替换，系统不会自动重复恢复。",
      });
    } catch (error) {
      if (!isAuthIdentityCurrent(identityEpoch)) {
        return;
      }
      setNotice({
        action: "verify-restore",
        kind: "error",
        text: `${adminErrorMessage(error)} 未能完成恢复结果核实，本地输入仍保留。`,
      });
    } finally {
      setVerificationPending(false);
    }
  };

  const handleCopy = async () => {
    if (!editor) {
      return;
    }
    try {
      await copyText(editor.draft);
      setCopyStatus("当前输入已复制到剪贴板。");
    } catch {
      setCopyStatus("浏览器未允许自动复制，请关闭弹窗后手动选择正文复制。");
    }
  };

  const cancelPendingConfirmation = () => {
    if (
      pendingConfirmation?.type === "route" &&
      blocker.state === "blocked"
    ) {
      blocker.reset();
    }
    setPendingConfirmation(null);
    setRouteSaveAttemptId(null);
    setCopyStatus(null);
  };

  const confirmDiscard = () => {
    if (!pendingConfirmation) {
      return;
    }
    if (pendingConfirmation.type === "switch-kind") {
      const kind = pendingConfirmation.kind;
      setPendingConfirmation(null);
      switchKind(kind);
      return;
    }
    if (pendingConfirmation.type === "route") {
      setPendingConfirmation(null);
      setRouteSaveAttemptId(null);
      if (blocker.state === "blocked") {
        blocker.proceed();
      }
      return;
    }
    if (pendingConfirmation.type === "restore") {
      void handleRestore(pendingConfirmation.source);
    }
  };

  const confirmationCopy = (() => {
    if (!pendingConfirmation) {
      return null;
    }
    if (pendingConfirmation.type === "switch-kind") {
      const nextKind = PROMPT_KINDS.find(
        (definition) => definition.kind === pendingConfirmation.kind,
      );
      return {
        confirmLabel: "放弃修改并切换",
        description:
          "当前正文有未保存修改。切换后将丢弃这份输入；需要保留时可先复制。",
        title: `切换到“${nextKind?.label ?? "另一类提示词"}”？`,
      };
    }
    if (pendingConfirmation.type === "route") {
      if (routeSaveAttempt?.status === "success") {
        return {
          confirmLabel: "仍然离开",
          description: `已保存，新任务会使用版本 ${routeSaveAttempt.version.id}。离开不会撤销保存结果，已有任务继续使用创建时固定的版本。${
            routeHasLaterEdits
              ? "保存完成后又产生了尚未提交的修改，离开会丢失这部分草稿。"
              : ""
          }`,
          title: "保存已完成，仍要离开？",
        };
      }
      if (
        routeSaveAttempt?.status === "pending" ||
        routeSaveAttempt?.status === "uncertain"
      ) {
        return {
          confirmLabel: "仍然离开",
          description: routeHasLaterEdits
            ? "保存请求已提交，离开不会撤销请求，旧请求仍可能生效。此后正文又产生了尚未提交的新修改，离开会丢失这些修改。请稍后返回此页并重新加载确认旧请求结果。"
            : "保存请求已提交，离开不会撤销请求，配置仍可能生效。请稍后返回此页并重新加载确认结果。",
          title:
            routeSaveAttempt.status === "pending"
              ? "保存请求处理中，仍要离开？"
              : "保存结果待确认，仍要离开？",
        };
      }
      if (routeSaveAttempt?.status === "rejected") {
        return {
          confirmLabel: "放弃并离开",
          description: routeHasLaterEdits
            ? "本次保存未成功，服务端没有确认切换版本；此后新增的修改也未提交，离开会丢失这些草稿。需要保留时请先复制。"
            : "本次保存未成功，服务端没有确认切换版本。当前修改仍未保存，离开将丢弃输入；需要保留时请先复制。",
          title: "保存未成功，仍要离开？",
        };
      }
      return {
        confirmLabel: "放弃并离开",
        description:
          "当前正文有未保存修改。离开页面后将丢弃这份输入；需要保留时可先复制。",
        title: "离开系统提示词页面？",
      };
    }
    if (pendingConfirmation.type === "reload") {
      return {
        confirmLabel: "替换并重新加载",
        description:
          "服务端可能已有其他管理员保存的新版本。重新加载会用服务端正文替换当前本地输入；需要保留时请先复制。",
        title: "重新载入当前版本？",
      };
    }
    return {
      confirmLabel: "恢复并生效",
      description:
        "恢复会以该历史正文创建新版本并立即对后续新任务生效，当前未保存编辑将被丢弃。",
      title: "恢复这个历史版本？",
    };
  })();

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
          <Icon
            name={notice.kind === "error" ? "shield" : "spark"}
            size={16}
          />
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
                <Icon name="refresh" size={14} />
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
          <ErrorBanner
            message={adminErrorMessage(current.error)}
            onRetry={() => void current.refetch()}
            retryLabel="重新读取"
          />
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
                  <ErrorBanner
                    message={`后台复核当前版本失败。${adminErrorMessage(current.error)}`}
                    onRetry={() => void current.refetch()}
                    retryLabel="重新读取"
                  />
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
                  onChange={(event) => {
                    const value = event.target.value;
                    setEditor((previous) =>
                      previous ? { ...previous, draft: value } : previous,
                    );
                    if (
                      routeSaveAttemptId === null &&
                      (saveAttempt?.status === "success" ||
                        saveAttempt?.status === "rejected")
                    ) {
                      setSaveAttempt(null);
                    }
                    // 恢复结果待核实时，继续编辑也不能移除唯一的非破坏性核实入口。
                    setNotice((previous) =>
                      previous?.action === "verify-restore"
                        ? previous
                        : null,
                    );
                  }}
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
                      onClick={() => {
                        setCopyStatus(null);
                        setPendingConfirmation({ type: "reload" });
                      }}
                      size="sm"
                      type="button"
                      variant="outline"
                    >
                      <Icon name="refresh" size={14} />
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
                      onClick={() => {
                        setCopyStatus(null);
                        setPendingConfirmation({ type: "reload" });
                      }}
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
                    {saveMutation.isPending ? "正在保存…" : "保存并生效"}
                  </Button>
                </div>
              </footer>
            </section>

            <section
              aria-labelledby="system-prompt-history-title"
              className={ADMIN_SECTION_CLASS}
            >
              <header className={ADMIN_SECTION_HEADING_CLASS}>
                <div>
                  <h2 id="system-prompt-history-title">版本历史</h2>
                </div>
                {history.isSuccess ? (
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
                <ErrorBanner
                  message={adminErrorMessage(history.error)}
                  onRetry={() => void history.refetch()}
                  retryLabel="重新读取历史"
                />
              ) : history.data.items.length === 0 ? (
                <p className={EMPTY_INLINE_CLASS}>后端尚未返回任何历史版本。</p>
              ) : (
                <>
                  <ol className="m-0 flex list-none flex-col gap-2 p-0">
                    {history.data.items.map((version) => {
                      const isCurrent = version.id === current.data?.id;
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
                                <Badge
                                  className="rounded-full"
                                  variant="success"
                                >
                                  当前生效
                                </Badge>
                              ) : null}
                              {version.restored_from_version_id ? (
                                <Badge
                                  className="rounded-full"
                                  variant="neutral"
                                >
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
                            onClick={() =>
                              setSelectedVersionId((selected) =>
                                selected === version.id ? null : version.id,
                              )
                            }
                            size="sm"
                            type="button"
                            variant="outline"
                          >
                            <Icon name="eye" size={14} />
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
                        <ErrorBanner
                          message={adminErrorMessage(selectedVersion.error)}
                          onRetry={() => void selectedVersion.refetch()}
                          retryLabel="重新读取正文"
                        />
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
                                selectedVersion.data.id === current.data?.id
                              }
                              onClick={() => {
                                setCopyStatus(null);
                                setPendingConfirmation({
                                  source: selectedVersion.data,
                                  type: "restore",
                                });
                              }}
                              size="sm"
                              type="button"
                              variant="outline"
                            >
                              <Icon name="history" size={14} />
                              {selectedVersion.data.id === current.data?.id
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
                    onPageChange={(nextPage) => {
                      setHistoryPage(nextPage);
                      setSelectedVersionId(null);
                    }}
                    page={historyPage}
                    pageSize={history.data.page_size}
                    total={history.data.total}
                  />
                </>
              )}
            </section>
          </div>
        ) : null}
      </div>

      <Modal
        closeDisabled={reloadPending || restoreMutation.isPending}
        description={confirmationCopy?.description}
        footer={
          <div className="flex flex-wrap justify-end gap-[7px] max-sm:[&>button]:flex-1">
            <Button
              disabled={reloadPending || restoreMutation.isPending}
              onClick={cancelPendingConfirmation}
              type="button"
              variant="outline"
            >
              {routeSaveAttempt?.status === "pending" &&
              pendingConfirmation?.type === "route"
                ? "留在此页"
                : "继续编辑"}
            </Button>
            <Button
              disabled={reloadPending || restoreMutation.isPending}
              onClick={() => void handleCopy()}
              type="button"
              variant="outline"
            >
              复制当前输入
            </Button>
            <Button
              disabled={reloadPending || restoreMutation.isPending}
              onClick={
                pendingConfirmation?.type === "reload"
                  ? () => void handleReloadCurrent()
                  : confirmDiscard
              }
              type="button"
              variant="primary"
            >
              {reloadPending || restoreMutation.isPending
                ? "正在处理…"
                : confirmationCopy?.confirmLabel}
            </Button>
          </div>
        }
        onClose={() => {
          if (!reloadPending && !restoreMutation.isPending) {
            cancelPendingConfirmation();
          }
        }}
        open={Boolean(pendingConfirmation)}
        panelClassName={MODAL_WIDTH_MD}
        title={confirmationCopy?.title ?? ""}
      >
        <p className="text-xs leading-[1.75] text-text-secondary">
          当前正文 {countCodePoints(editor?.draft ?? "")} 个字符。
          {copyStatus ? ` ${copyStatus}` : ""}
        </p>
      </Modal>
    </div>
  );
}
