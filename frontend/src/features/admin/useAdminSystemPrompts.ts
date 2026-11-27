import { useCallback, useEffect, useRef, useState } from "react";
import { useBlocker } from "react-router-dom";

import {
  ApiError,
  getAuthIdentityEpoch,
  isAuthIdentityCurrent,
} from "../../shared/api/client";
import { useSubmitLock } from "../../shared/lib/useSubmitLock";
import { useAuth } from "../auth/AuthProvider";
import { isSuperAdmin } from "../auth/model";
import { lastPageNumber } from "./AdminPagination";
import { adminErrorMessage } from "./errorMessages";
import {
  useRestoreSystemPromptMutation,
  useSaveSystemPromptMutation,
  useSystemPrompt,
  useSystemPromptVersion,
  useSystemPromptVersions,
} from "./hooks";
import {
  validateSystemPromptContent,
  type SystemPromptKind,
  type SystemPromptVersion,
} from "./model";

export const HISTORY_PAGE_SIZE = 10;

export const PROMPT_KINDS: Array<{
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

export interface EditorState {
  baselineContent: string;
  baselineCreatedAt: string;
  baselineCreatedBy: string | null;
  baselineId: string;
  draft: string;
  kind: SystemPromptKind;
}

export type PendingConfirmation =
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

export interface Notice {
  action?: "verify-restore";
  kind: "error" | "success";
  text: string;
}

interface SaveAttemptBase {
  id: number;
  submittedContent: string;
}

export type SaveAttempt =
  | (SaveAttemptBase & { status: "pending" })
  | (SaveAttemptBase & { status: "rejected" })
  | (SaveAttemptBase & { status: "uncertain" })
  | (SaveAttemptBase & {
      status: "success";
      version: SystemPromptVersion;
    });

export interface ConfirmationCopy {
  confirmLabel: string;
  description: string;
  title: string;
}

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

/** 展示版本修改来源（为空时统一回退为系统初始化） */
export function sourceLabel(createdBy: string | null): string {
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

/**
 * 系统提示词管理页核心状态 Hook：
 * - 管理主对话 / 历史压缩两类提示词切换、编辑器 CAS 基线与脏状态追踪；
 * - 承接路由离开拦截（useBlocker + beforeunload）及确认流；
 * - 分别以独立 useSubmitLock 保护保存、恢复、重载与核实请求，防止同 tick 连点且不误锁路由离开弹窗。
 */
export function useAdminSystemPrompts() {
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

  const saveLock = useSubmitLock();
  const restoreLock = useSubmitLock();
  const reloadLock = useSubmitLock();
  const verifyLock = useSubmitLock();

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
  // 保存与弹窗内操作共用全局动作互斥锁，但路由离开确认弹窗仅在重载或恢复期间禁止关闭。
  const modalActionPending =
    reloadPending ||
    restoreMutation.isPending ||
    reloadLock.isLocked ||
    restoreLock.isLocked;
  const actionPending =
    saveMutation.isPending ||
    saveLock.isLocked ||
    verificationPending ||
    verifyLock.isLocked ||
    modalActionPending;
  const saving = saveMutation.isPending || saveLock.isLocked;

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

  const handleDraftChange = (value: string) => {
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
      previous?.action === "verify-restore" ? previous : null,
    );
  };

  const handleSave = async () => {
    if (!editor || !isDirty || validationError || actionPending) {
      return;
    }
    const targetBaselineId = editor.baselineId;
    const submittedContent = editor.draft;
    await saveLock.runLocked(async () => {
      const identityEpoch = getAuthIdentityEpoch();
      const attemptId = ++nextSaveAttemptId.current;
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
          expectedCurrentVersionId: targetBaselineId,
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
        applyCurrentVersion(
          version,
          version.id === targetBaselineId
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
    });
  };

  const handleReloadCurrent = async () => {
    await reloadLock.runLocked(async () => {
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
    });
  };

  const handleRestore = async (source: SystemPromptVersion) => {
    if (!editor || actionPending) {
      return;
    }
    const targetBaselineId = editor.baselineId;
    await restoreLock.runLocked(async () => {
      const identityEpoch = getAuthIdentityEpoch();
      setNotice(null);
      try {
        const version = await restoreMutation.mutateAsync({
          expectedCurrentVersionId: targetBaselineId,
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
    });
  };

  const handleVerifyRestore = async () => {
    if (actionPending) {
      return;
    }
    await verifyLock.runLocked(async () => {
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
    });
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

  const openReloadConfirmation = () => {
    setCopyStatus(null);
    setPendingConfirmation({ type: "reload" });
  };

  const openRestoreConfirmation = (source: SystemPromptVersion) => {
    setCopyStatus(null);
    setPendingConfirmation({ source, type: "restore" });
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

  const confirmationCopy: ConfirmationCopy | null = (() => {
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

  const handleHistoryPageChange = (nextPage: number) => {
    setHistoryPage(nextPage);
    setSelectedVersionId(null);
  };

  const toggleSelectedVersion = (versionId: string) => {
    setSelectedVersionId((selected) =>
      selected === versionId ? null : versionId,
    );
  };

  return {
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
  };
}
