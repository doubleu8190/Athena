import { useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { PendingSessionUpload } from "../files/model";
import { sessionFileKeys } from "../files/queryKeys";
import { sessionKeys } from "../sessions/queryKeys";
import {
  answerRunInput,
  cancelRun,
  decideRunToolApproval,
  retryRun,
  submitRun,
} from "./api";
import { findQuestionText, type ChatAutoScrollRequest } from "./ChatMessageList";
import { useDraftChat } from "./DraftChatContext";
import { chatErrorMessage, requestIdCanBeReused } from "./errorMessages";
import { useLatestRun, useRunDetails, useSessionMessages } from "./hooks";
import {
  isActiveRunStatus,
  isTerminalRunStatus,
  mergeConversationMessages,
  runResultIsStale,
  selectRunProjection,
  shouldApplyStreamSnapshot,
  type ConversationMessagePage,
  type PendingSubagentApproval,
  type Run,
  type RunListResponse,
  type RunPendingApproval,
  type RunSnapshotPayload,
  type RunStatus,
} from "./model";
import { chatKeys } from "./queryKeys";
import { newRequestId } from "./requestId";
import {
  projectRunStatus,
  reconcileCompletedRun,
  reconcileFailedRun,
} from "./runStream";
import { subagentApprovalKey, subagentInputKey } from "./SubagentHitlPanels";
import {
  useRunStream,
  type RunStreamReconcileContext,
} from "./useRunStream";

interface FailedComposerCommand {
  fileIds: string[];
  inputRequestId?: string;
  kind: "answer" | "submit";
  knowledgeBaseIds: string[] | null;
  modelProfileId: string | null;
  requestId: string;
  reuseRequestId: boolean;
  skillIds: string[];
  text: string;
}

type RunCommandKind = "answer" | "approval" | "cancel" | "retry" | "submit";

interface ActiveRunCommand {
  kind: RunCommandKind;
  runId: string | null;
  token: number;
}

export interface TerminalMessageFailure {
  kind: "not_found" | "sync_failed";
  runId: string;
}

type MessageInfiniteData = InfiniteData<
  ConversationMessagePage,
  number | undefined
>;

type LiveAnnouncementPhase =
  | "starting"
  | "waiting_approval"
  | "waiting_input"
  | "completed"
  | "failed";

// SSE 高频 run_event 对账的合并节流窗口，避免连续事件触发查询失效请求风暴。
const RECONCILE_THROTTLE_MS = 500;

// 仅推进任务状态、工具调用进度、审批中间态、子智能体状态或上下文压缩的事件不会新增会话消息；
// 这类事件只失效 latestRun/run/toolCalls，避免触发 useInfiniteQuery 全历史消息页串行重拉。
const PROGRESS_ONLY_RUN_EVENT_TYPES: ReadonlySet<string> = new Set([
  "started",
  "tool_call_started",
  "tool_call_finished",
  "tool_approval_decided",
  "approval_resumed",
  "cancel_requested",
  "compression_started",
  "compression_completed",
  "compression_failed",
  "waiting_for_subagents",
  "subagents_resumed",
  "subagent_status_changed",
]);

function shouldReconcileMessages(
  context?: RunStreamReconcileContext,
): boolean {
  // 缺省上下文、显式 forceMessages、终态/等待态或未知新事件类型均保守全量失效消息流。
  if (!context || context.forceMessages || !context.eventType) {
    return true;
  }
  return !PROGRESS_ONLY_RUN_EVENT_TYPES.has(context.eventType);
}

// 首轮无标题会话完成后的有界退避静默复查计划（最多 2 次，title 到达或卸载立即清理）。
export const AUTO_TITLE_RETRY_DELAYS_MS = [1_000, 2_500] as const;

/** 精确对比知识库三态（null / [] / string[]），确保 request_id 幂等指纹与后端完全一致。 */
function knowledgeScopeEqual(
  left: string[] | null | undefined,
  right: string[] | null | undefined,
): boolean {
  const normalizedLeft = left ?? null;
  const normalizedRight = right ?? null;
  if (normalizedLeft === null || normalizedRight === null) {
    return normalizedLeft === normalizedRight;
  }
  return (
    normalizedLeft.length === normalizedRight.length &&
    normalizedLeft.every((id, index) => id === normalizedRight[index])
  );
}

function mergeReconciledMessages(
  current: MessageInfiniteData | undefined,
  fetched: {
    page: ConversationMessagePage;
    beforeSequence: number | undefined;
  }[],
): MessageInfiniteData {
  if (!current) {
    return {
      pageParams: fetched.map((item) => item.beforeSequence),
      pages: fetched.map((item) => item.page),
    };
  }
  const existingSequences = new Set(
    current.pages.flatMap((page) =>
      page.items.map((message) => message.sequence),
    ),
  );
  const additions = fetched.filter((item) =>
    item.page.items.some(
      (message) => !existingSequences.has(message.sequence),
    ),
  );
  if (additions.length === 0) {
    return current;
  }
  return {
    pageParams: [
      ...current.pageParams,
      ...additions.map((item) => item.beforeSequence),
    ],
    pages: [...current.pages, ...additions.map((item) => item.page)],
  };
}

export interface UseSessionChatControllerOptions {
  sessionId: string;
  /** 服务端会话标题：null 表示尚无标题，undefined 表示会话详情仍在加载中。 */
  sessionTitle: string | null | undefined;
}

/**
 * 会话工作台控制器 Hook：
 * 统一封装 SSE 实时流连接与单调快照投影、500ms 精细化节流对账、终态权威消息对账、
 * 命令共享锁与幂等重放指纹、多子智能体并发 HITL、首轮异步自动标题静默刷新
 * 以及仅在 5 个生命周期节点更新的 aria-live 无障碍通告。
 */
export function useSessionChatController({
  sessionId,
  sessionTitle,
}: UseSessionChatControllerOptions) {
  const queryClient = useQueryClient();
  const { clearSessionHandoff, getSessionHandoff } = useDraftChat();
  const initialHandoff = useMemo(
    () => getSessionHandoff(sessionId),
    [getSessionHandoff, sessionId],
  );

  const [selectedSkillIds, setSelectedSkillIds] = useState<string[]>(
    initialHandoff?.selectedSkillIds ?? [],
  );
  const [selectedFileIds, setSelectedFileIds] = useState<string[]>(
    initialHandoff?.inputFileIds ?? [],
  );
  const [knowledgeBaseIds, setKnowledgeBaseIds] = useState<string[] | null>(
    initialHandoff?.knowledgeBaseIds ?? null,
  );
  const [modelProfileId, setModelProfileId] = useState<string | null>(
    initialHandoff?.modelProfileId ?? null,
  );
  const [handoffUploads, setHandoffUploads] = useState<PendingSessionUpload[]>(
    initialHandoff?.pendingUploads ?? [],
  );
  const [hasBlockingUploads, setHasBlockingUploads] = useState(
    () => (initialHandoff?.pendingUploads?.length ?? 0) > 0,
  );
  const [commandError, setCommandError] = useState<string | null>(
    initialHandoff?.error ?? null,
  );
  const [commandNotice, setCommandNotice] = useState<string | null>(null);
  const [failedCommand, setFailedCommand] =
    useState<FailedComposerCommand | null>(
      initialHandoff
        ? {
            fileIds: initialHandoff.inputFileIds ?? [],
            kind: "submit",
            knowledgeBaseIds: initialHandoff.knowledgeBaseIds ?? null,
            modelProfileId: initialHandoff.modelProfileId ?? null,
            requestId: initialHandoff.requestId,
            reuseRequestId: initialHandoff.reuseRequestId,
            skillIds: initialHandoff.selectedSkillIds ?? [],
            text: initialHandoff.draft,
          }
        : null,
    );
  const [activeCommand, setActiveCommand] = useState<RunCommandKind | null>(
    null,
  );
  const [busyApprovalIds, setBusyApprovalIds] = useState<ReadonlySet<string>>(
    new Set(),
  );
  const [approvalErrors, setApprovalErrors] = useState<Record<string, string>>(
    {},
  );
  const [selectedSubagentInputKey, setSelectedSubagentInputKey] = useState<
    string | null
  >(null);
  const [busySubagentApprovalKeys, setBusySubagentApprovalKeys] = useState<
    ReadonlySet<string>
  >(new Set());
  const busySubagentApprovalKeysRef = useRef<Set<string>>(new Set());
  const [subagentApprovalErrors, setSubagentApprovalErrors] = useState<
    Record<string, string>
  >({});
  const subagentApprovalRequestsRef = useRef<
    Record<string, { decision: "approve" | "reject"; requestId: string }>
  >({});
  const [retryOpen, setRetryOpen] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [terminalMessageFailure, setTerminalMessageFailure] =
    useState<TerminalMessageFailure | null>(null);
  const [terminalMessageRetry, setTerminalMessageRetry] = useState(0);
  const [autoScrollRequest, setAutoScrollRequest] =
    useState<ChatAutoScrollRequest | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [liveAnnouncement, setLiveAnnouncement] = useState("");

  const approvalRequestsRef = useRef<
    Record<string, { decision: "approve" | "reject"; requestId: string }>
  >({});
  const commandLockRef = useRef<ActiveRunCommand | null>(null);
  const commandTokenRef = useRef(0);
  const runSnapshotRef = useRef<{
    run: Run | null;
    status: RunStatus | null;
    streamRunId: string | null;
    streamStatus: RunStatus | null;
  }>({
    run: null,
    status: null,
    streamRunId: null,
    streamStatus: null,
  });
  const retryRequestRef = useRef<{
    requestId: string;
    runId: string;
  } | null>(null);
  const settledTerminalRunsRef = useRef(new Set<string>());
  const reconcileTimerRef = useRef<number | null>(null);
  // 同一节流窗口内若先收到纯进度事件、后收到需刷新消息的事件，合并保留消息失效意图不丢失。
  const pendingMessagesReconcileRef = useRef(false);
  const suppressNextScheduledReconcileRef = useRef(false);
  // 记录运行中 live snapshot 已触发过结构化消息补拉的消息 ID，防止周期快照重复失效。
  const requestedSnapshotMessageIdsRef = useRef(new Set<string>());

  // 自动标题静默刷新状态：
  // - autoTitleFinishedRunIdsRef 仅在「标题已非空」或「全部退避次数执行完毕」后写入，
  //   避免 React.StrictMode 双挂载清理定时器后被误判为已完成而永久丢失退避调度；
  // - autoTitleImmediateRunIdsRef 仅用于去重首拍即时刷新，防止双挂载重复失效同一缓存。
  const latestSessionTitleRef = useRef<string | null | undefined>(sessionTitle);
  latestSessionTitleRef.current = sessionTitle;
  const autoTitleTimerRef = useRef<number | null>(null);
  const autoTitleFinishedRunIdsRef = useRef(new Set<string>());
  const autoTitleImmediateRunIdsRef = useRef(new Set<string>());

  // 仅在 5 个生命周期节点更新的无障碍播报追踪引用，杜绝按流式 Token 刷屏。
  const livePhaseKeyRef = useRef<string | null>(null);
  const observedActiveRunIdsRef = useRef(new Set<string>());

  const messagesQuery = useSessionMessages(sessionId);
  const {
    fetchNextPage: fetchNextMessagesPage,
    refetch: refetchMessages,
  } = messagesQuery;
  const handleLoadMoreMessages = useCallback(() => {
    void fetchNextMessagesPage();
  }, [fetchNextMessagesPage]);
  const handleRetryMessages = useCallback(() => {
    void refetchMessages();
  }, [refetchMessages]);

  const latestRuns = useLatestRun(sessionId);
  const {
    isError: latestRunsIsError,
    isSuccess: latestRunsIsSuccess,
    refetch: refetchLatestRunsQuery,
  } = latestRuns;
  const latestRun = latestRuns.data?.items[0] ?? null;
  const currentRunId = selectedRunId ?? latestRun?.id ?? null;
  const runDetails = useRunDetails(sessionId, currentRunId);
  const {
    isError: runDetailsIsError,
    refetch: refetchRunDetailsQuery,
  } = runDetails;
  const refetchLatestRuns = useCallback(() => {
    void refetchLatestRunsQuery();
  }, [refetchLatestRunsQuery]);
  const refetchRunDetails = useCallback(() => {
    void refetchRunDetailsQuery();
  }, [refetchRunDetailsQuery]);
  const retryTerminalReconcile = useCallback(() => {
    setTerminalMessageRetry((attempt) => attempt + 1);
  }, []);
  const run = selectRunProjection(
    currentRunId,
    latestRun,
    runDetails.data ?? null,
  );

  const clearReconcileTimer = useCallback(() => {
    if (reconcileTimerRef.current !== null) {
      window.clearTimeout(reconcileTimerRef.current);
      reconcileTimerRef.current = null;
    }
    pendingMessagesReconcileRef.current = false;
  }, []);

  const clearAutoTitleTimer = useCallback(() => {
    if (autoTitleTimerRef.current !== null) {
      window.clearTimeout(autoTitleTimerRef.current);
      autoTitleTimerRef.current = null;
    }
  }, []);

  const invalidateRunQueries = useCallback(
    (options?: { includeMessages?: boolean }) => {
      const includeMessages = options?.includeMessages ?? true;
      void queryClient.invalidateQueries({
        queryKey: chatKeys.latestRun(sessionId),
      });
      if (includeMessages) {
        void queryClient.invalidateQueries({
          queryKey: chatKeys.messages(sessionId),
        });
      }
      if (currentRunId) {
        void queryClient.invalidateQueries({
          queryKey: chatKeys.run(sessionId, currentRunId),
        });
        void queryClient.invalidateQueries({
          queryKey: chatKeys.toolCalls(sessionId, currentRunId),
        });
      }
    },
    [currentRunId, queryClient, sessionId],
  );

  // 命令纠偏以及终态/等待态快照需要零延迟全量刷新（含消息流），先清掉尚未触发的节流定时器避免重复请求。
  const flushReconcile = useCallback(() => {
    clearReconcileTimer();
    invalidateRunQueries({ includeMessages: true });
  }, [clearReconcileTimer, invalidateRunQueries]);

  // 高频连续 SSE run_event 在 500ms 窗口内合并，并按事件语义决定是否连带刷新历史消息流。
  const scheduleReconcile = useCallback(
    (context?: RunStreamReconcileContext) => {
      if (suppressNextScheduledReconcileRef.current) {
        suppressNextScheduledReconcileRef.current = false;
        return;
      }
      if (shouldReconcileMessages(context)) {
        pendingMessagesReconcileRef.current = true;
      }
      if (reconcileTimerRef.current !== null) {
        return;
      }
      reconcileTimerRef.current = window.setTimeout(() => {
        reconcileTimerRef.current = null;
        const includeMessages = pendingMessagesReconcileRef.current;
        pendingMessagesReconcileRef.current = false;
        invalidateRunQueries({ includeMessages });
      }, RECONCILE_THROTTLE_MS);
    },
    [invalidateRunQueries],
  );

  useEffect(() => {
    // 卸载或切换 sessionId / currentRunId 时立即清理对账节流定时器与快照消息补拉去重集。
    const requestedSnapshotMessageIds = requestedSnapshotMessageIdsRef.current;
    return () => {
      suppressNextScheduledReconcileRef.current = false;
      requestedSnapshotMessageIds.clear();
      clearReconcileTimer();
    };
  }, [clearReconcileTimer, currentRunId, sessionId]);

  const handleStreamSnapshot = useCallback(
    (snapshot: RunSnapshotPayload) => {
      if (!currentRunId) {
        return;
      }
      const existingRun = selectRunProjection(
        currentRunId,
        queryClient
          .getQueryData<RunListResponse>(chatKeys.latestRun(sessionId))
          ?.items.find((item) => item.id === currentRunId) ?? null,
        queryClient.getQueryData<Run>(chatKeys.run(sessionId, currentRunId)) ??
          null,
      );
      const snapshotAccepted =
        !existingRun || shouldApplyStreamSnapshot(existingRun, snapshot);
      // SSE 快照包含子任务与并发 HITL 最新投影；写入前校验单调性，禁止终态、取消态或旧序号回退覆盖。
      const patchRun = (existing: Run): Run =>
        shouldApplyStreamSnapshot(existing, snapshot)
          ? {
              ...existing,
              event_sequence: snapshot.event_sequence,
              pending_subagent_approvals:
                snapshot.pending_subagent_approvals ?? [],
              pending_subagent_inputs: snapshot.pending_subagent_inputs ?? [],
              report_message_id: snapshot.report_message_id,
              status: snapshot.status,
              subagent_tasks: snapshot.subagent_tasks ?? [],
            }
          : existing;
      queryClient.setQueryData<Run>(
        chatKeys.run(sessionId, currentRunId),
        (existing) => (existing ? patchRun(existing) : existing),
      );
      queryClient.setQueryData<RunListResponse>(
        chatKeys.latestRun(sessionId),
        (existing) =>
          existing
            ? {
                ...existing,
                items: existing.items.map((item) =>
                  item.id === currentRunId ? patchRun(item) : item,
                ),
              }
            : existing,
      );
      // 终态或等待态快照随后会关闭 SSE，立即刷新对账并跳过 useRunStream 紧接着触发的重复节流回调。
      if (
        isTerminalRunStatus(snapshot.status) ||
        snapshot.status === "waiting_for_input" ||
        snapshot.status === "waiting_for_approval"
      ) {
        suppressNextScheduledReconcileRef.current = true;
        flushReconcile();
        return;
      }
      // 运行中快照仅承载纯文本投影（不含 FileRefPart 等结构化附件块）。
      // 当主 Run 在运行期间写入 Skill 交付消息并在快照中出现尚未缓存的新消息 ID 时，
      // 精准失效消息流与会话文件列表以补齐附件卡片并同步文件抽屉，同时保持普通无产物进度事件零重拉。
      if (!snapshotAccepted || snapshot.messages.length === 0) {
        return;
      }
      const cachedMessages = queryClient.getQueryData<MessageInfiniteData>(
        chatKeys.messages(sessionId),
      );
      // 首屏消息缓存尚未就绪时不重复失效，避免冷启动首帧并发重拉。
      if (!cachedMessages) {
        return;
      }
      const cachedMessageIds = new Set<string>();
      for (const page of cachedMessages.pages) {
        for (const message of page.items) {
          cachedMessageIds.add(message.id);
        }
      }
      const unseenMessageIds: string[] = [];
      for (const message of snapshot.messages) {
        if (
          !cachedMessageIds.has(message.id) &&
          !requestedSnapshotMessageIdsRef.current.has(message.id)
        ) {
          unseenMessageIds.push(message.id);
        }
      }
      if (unseenMessageIds.length === 0) {
        return;
      }
      for (const messageId of unseenMessageIds) {
        requestedSnapshotMessageIdsRef.current.add(messageId);
      }
      void queryClient.invalidateQueries({
        queryKey: chatKeys.messages(sessionId),
      });
      void queryClient.invalidateQueries({
        queryKey: sessionFileKeys.list(sessionId),
      });
    },
    [currentRunId, flushReconcile, queryClient, sessionId],
  );

  const stream = useRunStream({
    onReconcile: scheduleReconcile,
    onSnapshot: handleStreamSnapshot,
    runId: currentRunId,
    sessionId,
    status: run?.status ?? null,
  });
  const {
    closeTerminal: closeStreamTerminal,
    settleTerminal: settleStreamTerminal,
    syncStatus: syncStreamStatus,
  } = stream;
  const streamMatchesRun = stream.state.runId === currentRunId;
  const streamEvents = stream.state.events;
  const streamReportMessageId = streamMatchesRun
    ? stream.state.reportMessageId
    : null;
  const transientText = streamMatchesRun ? stream.state.transientText : "";
  // SSE 先驱动中间状态；服务端查询确认终态后，终态必须覆盖残留的 live 状态。
  const status = projectRunStatus({
    queryStatus: run?.status ?? null,
    runId: currentRunId,
    streamRunId: stream.state.runId,
    streamStatus: stream.state.status,
  });
  const isActive = isActiveRunStatus(status);
  const isWaitingInput = status === "waiting_for_input";
  const isWaitingApproval = status === "waiting_for_approval";
  const isWaitingSubagents = status === "waiting_for_subagents";

  const subagentTasks = useMemo(
    () =>
      run?.subagent_tasks ??
      (streamMatchesRun && stream.state.hasSnapshot
        ? stream.state.subagentTasks
        : []),
    [
      run?.subagent_tasks,
      stream.state.hasSnapshot,
      stream.state.subagentTasks,
      streamMatchesRun,
    ],
  );
  const pendingSubagentInputs = useMemo(
    () =>
      run?.pending_subagent_inputs ??
      (streamMatchesRun && stream.state.hasSnapshot
        ? stream.state.pendingSubagentInputs
        : []),
    [
      run?.pending_subagent_inputs,
      stream.state.hasSnapshot,
      stream.state.pendingSubagentInputs,
      streamMatchesRun,
    ],
  );
  const pendingSubagentApprovals = useMemo(
    () =>
      run?.pending_subagent_approvals ??
      (streamMatchesRun && stream.state.hasSnapshot
        ? stream.state.pendingSubagentApprovals
        : []),
    [
      run?.pending_subagent_approvals,
      stream.state.hasSnapshot,
      stream.state.pendingSubagentApprovals,
      streamMatchesRun,
    ],
  );

  const activeSubagentInput = useMemo(() => {
    if (pendingSubagentInputs.length === 0) {
      return null;
    }
    if (selectedSubagentInputKey) {
      const matched = pendingSubagentInputs.find(
        (item) => subagentInputKey(item) === selectedSubagentInputKey,
      );
      if (matched) {
        return matched;
      }
    }
    return pendingSubagentInputs[0];
  }, [pendingSubagentInputs, selectedSubagentInputKey]);
  const isTargetedSubagentAnswer = Boolean(activeSubagentInput);
  const isLatestRun = Boolean(run && latestRun?.id === run.id);
  const sending = activeCommand === "submit" || activeCommand === "answer";
  const stopping = activeCommand === "cancel";
  const retrying = activeCommand === "retry";
  const commandPending = activeCommand !== null;

  runSnapshotRef.current = {
    run,
    status,
    streamRunId: stream.state.runId,
    streamStatus: stream.state.status,
  };

  // 首轮异步自动标题静默刷新：
  // 1) 一旦 session.title 变为非空（自动标题到达或用户手动改名），立即停止并标记本轮复查完成；
  // 2) 当会话已加载且 title === null 且主 Run 进入 completed 时，先立刻通过 sessionKeys.all
  //    一次性失效会话详情与侧栏列表缓存，再按 1000ms -> 2500ms 最多静默复查 2 次；
  // 3) 仅在标题非空或 2 次退避全部执行完毕后写入完成集合，确保 StrictMode 双挂载清理定时器后能恢复调度。
  useEffect(() => {
    if (typeof sessionTitle === "string" && sessionTitle.trim().length > 0) {
      clearAutoTitleTimer();
      if (currentRunId) {
        autoTitleFinishedRunIdsRef.current.add(`${sessionId}:${currentRunId}`);
      }
      return;
    }

    if (!currentRunId || status !== "completed" || sessionTitle !== null) {
      clearAutoTitleTimer();
      return;
    }

    const autoTitleKey = `${sessionId}:${currentRunId}`;
    if (autoTitleFinishedRunIdsRef.current.has(autoTitleKey)) {
      return;
    }

    let cancelled = false;
    const refreshSessionsSilently = () => {
      if (cancelled || latestSessionTitleRef.current !== null) {
        clearAutoTitleTimer();
        return;
      }
      // sessionKeys.all (["sessions"]) 已覆盖 session detail 与会话列表，避免重复失效。
      void queryClient.invalidateQueries({
        queryKey: sessionKeys.all,
      });
    };

    if (!autoTitleImmediateRunIdsRef.current.has(autoTitleKey)) {
      autoTitleImmediateRunIdsRef.current.add(autoTitleKey);
      refreshSessionsSilently();
    }

    const scheduleNextRetry = (attemptIndex: number) => {
      if (cancelled) {
        return;
      }
      if (attemptIndex >= AUTO_TITLE_RETRY_DELAYS_MS.length) {
        autoTitleFinishedRunIdsRef.current.add(autoTitleKey);
        return;
      }
      clearAutoTitleTimer();
      autoTitleTimerRef.current = window.setTimeout(() => {
        autoTitleTimerRef.current = null;
        if (cancelled) {
          return;
        }
        if (latestSessionTitleRef.current !== null) {
          if (
            typeof latestSessionTitleRef.current === "string" &&
            latestSessionTitleRef.current.trim().length > 0
          ) {
            autoTitleFinishedRunIdsRef.current.add(autoTitleKey);
          }
          return;
        }
        refreshSessionsSilently();
        scheduleNextRetry(attemptIndex + 1);
      }, AUTO_TITLE_RETRY_DELAYS_MS[attemptIndex]);
    };

    scheduleNextRetry(0);

    return () => {
      cancelled = true;
      clearAutoTitleTimer();
    };
  }, [
    clearAutoTitleTimer,
    currentRunId,
    queryClient,
    sessionId,
    sessionTitle,
    status,
  ]);

  // 5 节点无障碍状态通告：仅在开始生成 / 等待工具审批 / 等待补充信息 / 生成完成 / 执行失败变化时更新，绝不按流式 Token 刷屏。
  const hasPendingSubagentApprovals = pendingSubagentApprovals.length > 0;
  const hasPendingSubagentInputs = pendingSubagentInputs.length > 0;
  useEffect(() => {
    if (!currentRunId || !status) {
      return;
    }
    if (isActiveRunStatus(status)) {
      observedActiveRunIdsRef.current.add(currentRunId);
    }

    let phase: LiveAnnouncementPhase | null = null;
    let message = "";
    if (status === "waiting_for_approval" || hasPendingSubagentApprovals) {
      phase = "waiting_approval";
      message = "等待工具审批，请确认操作";
    } else if (status === "waiting_for_input" || hasPendingSubagentInputs) {
      phase = "waiting_input";
      message = "等待补充信息，请回复后继续";
    } else if (
      status === "queued" ||
      status === "running" ||
      status === "waiting_for_subagents"
    ) {
      phase = "starting";
      message = "开始生成回复";
    } else if (
      status === "completed" &&
      observedActiveRunIdsRef.current.has(currentRunId)
    ) {
      phase = "completed";
      message = "回复已生成完成";
    } else if (
      status === "failed" &&
      observedActiveRunIdsRef.current.has(currentRunId)
    ) {
      phase = "failed";
      message = "任务执行失败";
    }

    if (!phase) {
      return;
    }
    const phaseKey = `${currentRunId}:${phase}`;
    if (livePhaseKeyRef.current === phaseKey) {
      return;
    }
    livePhaseKeyRef.current = phaseKey;
    setLiveAnnouncement(message);
  }, [
    currentRunId,
    hasPendingSubagentApprovals,
    hasPendingSubagentInputs,
    status,
  ]);

  // 终态对账：关闭 SSE 流、刷新会话文件列表，并按权威事件 ID 对账最终回答或失败报告后再清理瞬时流式文本。
  useEffect(() => {
    if (!currentRunId || !status || !isTerminalRunStatus(status)) {
      return;
    }
    const terminalRunId = currentRunId;
    const terminalStatus = status;
    const triggerMessageId = run?.trigger_message_id;
    closeStreamTerminal(terminalRunId, terminalStatus);
    if (reconcileTimerRef.current !== null) {
      flushReconcile();
    }
    // 刷新和终态恢复都重新读取服务端文件发布记录，确保产物立即同步至文件抽屉与附件菜单。
    void queryClient.invalidateQueries({
      queryKey: sessionFileKeys.list(sessionId),
    });
    // 取消终态没有权威回答；旧工具/文件交付消息不能用于清除临时文本。
    if (terminalStatus === "cancelled") {
      return;
    }
    if (!triggerMessageId) {
      setTerminalMessageFailure({
        kind: "sync_failed",
        runId: terminalRunId,
      });
      return;
    }
    const terminalKey = `${terminalRunId}:${terminalStatus}`;
    if (settledTerminalRunsRef.current.has(terminalKey)) {
      return;
    }
    let active = true;
    setTerminalMessageFailure(null);
    const projectedReportMessageId =
      terminalStatus === "failed"
        ? (run?.report_message_id ?? streamReportMessageId)
        : null;
    const reconciliation =
      terminalStatus === "failed"
        ? reconcileFailedRun(
            sessionId,
            terminalRunId,
            triggerMessageId,
            projectedReportMessageId,
            streamEvents,
          )
        : reconcileCompletedRun(
            sessionId,
            terminalRunId,
            triggerMessageId,
            streamEvents,
          );
    void reconciliation
      .then((result) => {
        if (result.kind === "not_found") {
          // 普通 failed 没有报告时维持原行为；只有显式报告未能对账才提示失败。
          if (
            terminalStatus === "failed" &&
            result.reason === "failed_event"
          ) {
            return;
          }
          if (active) {
            setTerminalMessageFailure({
              kind: "not_found",
              runId: terminalRunId,
            });
          }
          return;
        }
        if (!active) {
          return;
        }
        queryClient.setQueryData<MessageInfiniteData>(
          chatKeys.messages(sessionId),
          (current) => mergeReconciledMessages(current, result.messages),
        );
        settledTerminalRunsRef.current.add(terminalKey);
        settleStreamTerminal(terminalRunId, terminalStatus);
      })
      .catch(() => {
        if (active) {
          setTerminalMessageFailure({
            kind: "sync_failed",
            runId: terminalRunId,
          });
        }
      });
    return () => {
      active = false;
    };
  }, [
    closeStreamTerminal,
    currentRunId,
    flushReconcile,
    queryClient,
    run?.report_message_id,
    run?.trigger_message_id,
    sessionId,
    settleStreamTerminal,
    status,
    streamEvents,
    streamReportMessageId,
    terminalMessageRetry,
  ]);

  // 同一 Run 的所有用户命令共享一个显式锁；ref 在事件入口同步生效，activeCommand 驱动 UI 禁用态。
  const beginRunCommand = useCallback(
    (kind: RunCommandKind, runId: string | null): number | null => {
      if (commandLockRef.current) {
        return null;
      }
      const token = commandTokenRef.current + 1;
      commandTokenRef.current = token;
      commandLockRef.current = { kind, runId, token };
      setCommandNotice(null);
      setActiveCommand(kind);
      return token;
    },
    [],
  );

  const endRunCommand = useCallback((token: number) => {
    if (commandLockRef.current?.token !== token) {
      return;
    }
    commandLockRef.current = null;
    setActiveCommand(null);
  }, []);

  const isCurrentRunCommand = useCallback(
    (token: number) => commandLockRef.current?.token === token,
    [],
  );

  const messages = useMemo(
    () =>
      mergeConversationMessages(
        messagesQuery.data?.pages ?? [],
        currentRunId,
        streamMatchesRun ? stream.state.messages : [],
      ),
    [
      currentRunId,
      messagesQuery.data?.pages,
      stream.state.messages,
      streamMatchesRun,
    ],
  );

  const questionText = useMemo(() => {
    const questionMessageId = run?.pending_input?.question_message_id;
    return questionMessageId
      ? findQuestionText(messages, questionMessageId)
      : null;
  }, [messages, run?.pending_input?.question_message_id]);

  const applyRunResult = useCallback(
    (nextRun: Run, commandToken: number): boolean => {
      // 结果只允许发起它的当前命令应用，防止迟到异步链覆盖后续命令状态。
      if (!isCurrentRunCommand(commandToken)) {
        flushReconcile();
        return false;
      }
      const locked = commandLockRef.current;
      if (
        locked?.runId &&
        (locked.kind === "answer" ||
          locked.kind === "approval" ||
          locked.kind === "cancel") &&
        locked.runId !== nextRun.id
      ) {
        flushReconcile();
        return false;
      }
      const liveState = runSnapshotRef.current;
      if (
        liveState.streamRunId === nextRun.id &&
        (liveState.streamStatus === "cancelling" ||
          liveState.streamStatus === "cancelled") &&
        nextRun.status !== "cancelling" &&
        nextRun.status !== "cancelled"
      ) {
        flushReconcile();
        return false;
      }

      const latestPage = queryClient.getQueryData<RunListResponse>(
        chatKeys.latestRun(sessionId),
      );
      const cachedRuns = [
        latestPage?.items.find((item) => item.id === nextRun.id),
        queryClient.getQueryData<Run>(chatKeys.run(sessionId, nextRun.id)),
        runSnapshotRef.current.run?.id === nextRun.id
          ? runSnapshotRef.current.run
          : null,
      ].filter((item): item is Run => Boolean(item));
      const currentRun = cachedRuns.reduce<Run | null>((current, candidate) => {
        if (!current) {
          return candidate;
        }
        if (candidate.execution_epoch !== current.execution_epoch) {
          return candidate.execution_epoch > current.execution_epoch
            ? candidate
            : current;
        }
        if (candidate.event_sequence !== current.event_sequence) {
          return candidate.event_sequence > current.event_sequence
            ? candidate
            : current;
        }
        return Date.parse(candidate.updated_at) > Date.parse(current.updated_at)
          ? candidate
          : current;
      }, null);
      if (currentRun && runResultIsStale(nextRun, currentRun)) {
        flushReconcile();
        return false;
      }

      setSelectedRunId(nextRun.id);
      syncStreamStatus(nextRun.id, nextRun.status);
      queryClient.setQueryData(chatKeys.latestRun(sessionId), {
        items: [nextRun],
        next_cursor: null,
      });
      queryClient.setQueryData(chatKeys.run(sessionId, nextRun.id), nextRun);
      void queryClient.invalidateQueries({
        queryKey: chatKeys.latestRun(sessionId),
      });
      void queryClient.invalidateQueries({
        queryKey: chatKeys.messages(sessionId),
      });
      void queryClient.invalidateQueries({
        queryKey: chatKeys.run(sessionId, nextRun.id),
      });
      void queryClient.invalidateQueries({
        queryKey: chatKeys.toolCalls(sessionId, nextRun.id),
      });
      return true;
    },
    [
      flushReconcile,
      isCurrentRunCommand,
      queryClient,
      sessionId,
      syncStreamStatus,
    ],
  );

  // 稳定提交回调引用，避免高频流式 text_delta 导致 memo 化的 SessionComposer 无效重渲染。
  const handleSubmit = useCallback(
    async (submittedDraft: string): Promise<boolean> => {
      const text = submittedDraft.trim();
      if (!text || commandLockRef.current) {
        return false;
      }
      if (hasBlockingUploads || handoffUploads.length > 0) {
        setCommandError(
          "请先等待附件上传完成，或重试/移除失败的附件后再提交。",
        );
        return false;
      }
      if (isWaitingInput && !run?.pending_input) {
        setCommandError("服务端尚未返回可回答的输入请求，正在同步状态。");
        flushReconcile();
        return false;
      }
      if (isActive && !isWaitingInput && !isTargetedSubagentAnswer) {
        return false;
      }
      if (!run && !latestRunsIsSuccess) {
        return false;
      }

      const inputRequestId = activeSubagentInput
        ? activeSubagentInput.input_request_id
        : isWaitingInput
          ? run?.pending_input?.id
          : undefined;
      const targetRunId = activeSubagentInput
        ? activeSubagentInput.sub_run_id
        : (run?.id ?? null);
      const sequenceAtSubmit = messages.reduce(
        (latestSequence, message) => Math.max(latestSequence, message.sequence),
        0,
      );
      const isAnswering = isWaitingInput || isTargetedSubagentAnswer;
      const submittedSkillIds = isAnswering ? [] : [...selectedSkillIds];
      const submittedFileIds = [...selectedFileIds];
      const submittedKnowledgeBaseIds = isAnswering
        ? null
        : knowledgeBaseIds === null
          ? null
          : [...knowledgeBaseIds];
      const submittedModelProfileId = isAnswering ? null : modelProfileId;
      const commandKind: RunCommandKind = isAnswering ? "answer" : "submit";
      const commandToken = beginRunCommand(commandKind, targetRunId);
      if (commandToken === null) {
        return false;
      }
      const canReuse =
        failedCommand?.reuseRequestId === true &&
        failedCommand?.text === text &&
        failedCommand.fileIds.join("\u0000") ===
          submittedFileIds.join("\u0000") &&
        failedCommand.skillIds.join("\u0000") ===
          submittedSkillIds.join("\u0000") &&
        knowledgeScopeEqual(
          failedCommand.knowledgeBaseIds,
          submittedKnowledgeBaseIds,
        ) &&
        (failedCommand.modelProfileId ?? null) ===
          (submittedModelProfileId ?? null) &&
        (isAnswering
          ? failedCommand.kind === "answer" &&
            failedCommand.inputRequestId === inputRequestId
          : failedCommand?.kind === "submit");
      const requestId = canReuse ? failedCommand.requestId : newRequestId();

      setCommandError(null);
      try {
        const nextRun =
          activeSubagentInput && inputRequestId
            ? await answerRunInput(
                sessionId,
                activeSubagentInput.sub_run_id,
                inputRequestId,
                requestId,
                text,
                submittedFileIds,
              )
            : isWaitingInput && run && inputRequestId
              ? await answerRunInput(
                  sessionId,
                  run.id,
                  inputRequestId,
                  requestId,
                  text,
                  submittedFileIds,
                )
              : await submitRun(
                  sessionId,
                  requestId,
                  text,
                  submittedSkillIds,
                  submittedFileIds,
                  {
                    knowledgeBaseIds: submittedKnowledgeBaseIds,
                    modelProfileId: submittedModelProfileId,
                  },
                );

        if (activeSubagentInput) {
          // 子智能体定向回答返回的是子 Run，不能切换 selectedRunId；
          // 若回答接口未携带完整 input_interactions，则乐观保留并更新已缓存的提问记录再刷新详情。
          queryClient.setQueryData<Run>(
            chatKeys.run(sessionId, nextRun.id),
            (existing) => {
              if (
                !nextRun.input_interactions?.length &&
                existing?.input_interactions?.length
              ) {
                return {
                  ...nextRun,
                  input_interactions: existing.input_interactions.map((item) =>
                    item.input_request_id === activeSubagentInput.input_request_id
                      ? {
                          ...item,
                          answer_text: text,
                          answered_at:
                            item.answered_at ?? new Date().toISOString(),
                          status: "answered",
                        }
                      : item,
                  ),
                };
              }
              return nextRun;
            },
          );
          void queryClient.invalidateQueries({
            queryKey: chatKeys.run(sessionId, nextRun.id),
          });
          void queryClient.invalidateQueries({
            queryKey: chatKeys.toolCalls(sessionId, nextRun.id),
          });
          if (currentRunId) {
            const removeAnswered = (existing: Run): Run => ({
              ...existing,
              pending_subagent_inputs: (
                existing.pending_subagent_inputs ?? []
              ).filter(
                (item) =>
                  item.input_request_id !== activeSubagentInput.input_request_id,
              ),
            });
            queryClient.setQueryData<Run>(
              chatKeys.run(sessionId, currentRunId),
              (existing) => (existing ? removeAnswered(existing) : existing),
            );
            queryClient.setQueryData<RunListResponse>(
              chatKeys.latestRun(sessionId),
              (existing) =>
                existing
                  ? {
                      ...existing,
                      items: existing.items.map((item) =>
                        item.id === currentRunId ? removeAnswered(item) : item,
                      ),
                    }
                  : existing,
            );
          }
          flushReconcile();
        } else {
          applyRunResult(nextRun, commandToken);
        }

        if (isCurrentRunCommand(commandToken)) {
          setFailedCommand(null);
          setSelectedFileIds([]);
          setSelectedSkillIds([]);
          setHandoffUploads([]);
          clearSessionHandoff(sessionId);
          if (!activeSubagentInput) {
            setAutoScrollRequest(
              isAnswering
                ? {
                    afterSequence: sequenceAtSubmit,
                    key: `${commandToken}:${requestId}`,
                    text,
                  }
                : {
                    key: `${commandToken}:${requestId}`,
                    messageId: nextRun.trigger_message_id,
                  },
            );
          }
          setCommandNotice(
            activeSubagentInput
              ? `已向「${activeSubagentInput.subagent_name}」提交回复，正在继续处理。`
              : isWaitingInput
                ? "补充回答已提交，正在按服务端最新运行状态同步。"
                : "消息已提交，正在按服务端最新运行状态同步。",
          );
          return true;
        }
        return false;
      } catch (error) {
        if (isCurrentRunCommand(commandToken)) {
          setFailedCommand({
            fileIds: submittedFileIds,
            inputRequestId,
            kind: isAnswering ? "answer" : "submit",
            knowledgeBaseIds: submittedKnowledgeBaseIds,
            modelProfileId: submittedModelProfileId,
            requestId,
            reuseRequestId: requestIdCanBeReused(error),
            skillIds: submittedSkillIds,
            text,
          });
          setCommandNotice(null);
          setCommandError(chatErrorMessage(error));
          flushReconcile();
        }
        return false;
      } finally {
        endRunCommand(commandToken);
      }
    },
    [
      activeSubagentInput,
      applyRunResult,
      beginRunCommand,
      clearSessionHandoff,
      currentRunId,
      endRunCommand,
      failedCommand,
      flushReconcile,
      handoffUploads.length,
      hasBlockingUploads,
      isActive,
      isCurrentRunCommand,
      isTargetedSubagentAnswer,
      isWaitingInput,
      knowledgeBaseIds,
      latestRunsIsSuccess,
      messages,
      modelProfileId,
      queryClient,
      run,
      selectedFileIds,
      selectedSkillIds,
      sessionId,
    ],
  );

  const handleCancel = useCallback(async () => {
    if (!run || commandLockRef.current) {
      return;
    }
    const commandToken = beginRunCommand("cancel", run.id);
    if (commandToken === null) {
      return;
    }
    setCommandError(null);
    try {
      const nextRun = await cancelRun(sessionId, run.id);
      applyRunResult(nextRun, commandToken);
    } catch (error) {
      if (isCurrentRunCommand(commandToken)) {
        setCommandError(chatErrorMessage(error));
        flushReconcile();
      }
    } finally {
      endRunCommand(commandToken);
    }
  }, [
    applyRunResult,
    beginRunCommand,
    endRunCommand,
    flushReconcile,
    isCurrentRunCommand,
    run,
    sessionId,
  ]);

  const activeRunId = run?.id ?? null;

  const handleApproval = useCallback(
    async (
      approval: RunPendingApproval,
      decision: "approve" | "reject",
    ) => {
      if (!activeRunId || commandLockRef.current) {
        return;
      }
      const commandToken = beginRunCommand("approval", activeRunId);
      if (commandToken === null) {
        return;
      }
      // 审批响应包含整份 Run 投影；串行提交避免较早响应迟到后覆盖最后一次决定产生的 queued 状态。
      const previous = approvalRequestsRef.current[approval.call_id];
      const requestId =
        previous?.decision === decision ? previous.requestId : newRequestId();
      approvalRequestsRef.current[approval.call_id] = {
        decision,
        requestId,
      };
      setApprovalErrors((current) => {
        const next = { ...current };
        delete next[approval.call_id];
        return next;
      });
      setBusyApprovalIds((current) => {
        const next = new Set(current);
        next.add(approval.call_id);
        return next;
      });
      try {
        const nextRun = await decideRunToolApproval(
          sessionId,
          activeRunId,
          approval.call_id,
          requestId,
          decision,
        );
        delete approvalRequestsRef.current[approval.call_id];
        setApprovalErrors((current) => {
          const next = { ...current };
          delete next[approval.call_id];
          return next;
        });
        setCommandError(null);
        applyRunResult(nextRun, commandToken);
      } catch (error) {
        if (isCurrentRunCommand(commandToken)) {
          setApprovalErrors((current) => ({
            ...current,
            [approval.call_id]: chatErrorMessage(error),
          }));
          flushReconcile();
        }
      } finally {
        if (isCurrentRunCommand(commandToken)) {
          setBusyApprovalIds((current) => {
            const next = new Set(current);
            next.delete(approval.call_id);
            return next;
          });
        }
        endRunCommand(commandToken);
      }
    },
    [
      activeRunId,
      applyRunResult,
      beginRunCommand,
      endRunCommand,
      flushReconcile,
      isCurrentRunCommand,
      sessionId,
    ],
  );

  const handleSubagentApproval = useCallback(
    async (
      approval: PendingSubagentApproval,
      decision: "approve" | "reject",
    ) => {
      const key = subagentApprovalKey(approval);
      // 同步 ref 锁防止同一渲染帧内连点触发重复审批请求，同时避免依赖 state 集合破坏记忆化。
      if (busySubagentApprovalKeysRef.current.has(key)) {
        return;
      }
      busySubagentApprovalKeysRef.current.add(key);
      const previous = subagentApprovalRequestsRef.current[key];
      const requestId =
        previous?.decision === decision ? previous.requestId : newRequestId();
      subagentApprovalRequestsRef.current[key] = { decision, requestId };
      setSubagentApprovalErrors((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      setBusySubagentApprovalKeys((current) => {
        const next = new Set(current);
        next.add(key);
        return next;
      });
      try {
        // 子智能体工具审批精确路由至对应 sub_run_id，各子智能体之间互不阻塞。
        const nextSubRun = await decideRunToolApproval(
          sessionId,
          approval.sub_run_id,
          approval.call_id,
          requestId,
          decision,
        );
        delete subagentApprovalRequestsRef.current[key];
        queryClient.setQueryData(
          chatKeys.run(sessionId, nextSubRun.id),
          nextSubRun,
        );
        if (currentRunId) {
          const removeDecided = (existing: Run): Run => ({
            ...existing,
            pending_subagent_approvals: (
              existing.pending_subagent_approvals ?? []
            ).filter(
              (item) =>
                !(
                  item.sub_run_id === approval.sub_run_id &&
                  item.call_id === approval.call_id
                ),
            ),
          });
          queryClient.setQueryData<Run>(
            chatKeys.run(sessionId, currentRunId),
            (existing) => (existing ? removeDecided(existing) : existing),
          );
          queryClient.setQueryData<RunListResponse>(
            chatKeys.latestRun(sessionId),
            (existing) =>
              existing
                ? {
                    ...existing,
                    items: existing.items.map((item) =>
                      item.id === currentRunId ? removeDecided(item) : item,
                    ),
                  }
                : existing,
          );
        }
        flushReconcile();
      } catch (error) {
        setSubagentApprovalErrors((current) => ({
          ...current,
          [key]: chatErrorMessage(error),
        }));
        flushReconcile();
      } finally {
        busySubagentApprovalKeysRef.current.delete(key);
        setBusySubagentApprovalKeys((current) => {
          const next = new Set(current);
          next.delete(key);
          return next;
        });
      }
    },
    [currentRunId, flushReconcile, queryClient, sessionId],
  );

  const openRetryDialog = useCallback(() => {
    setRetryError(null);
    setRetryOpen(true);
  }, []);

  const closeRetryDialog = useCallback(() => {
    setRetryOpen(false);
  }, []);

  const handleRetry = useCallback(async () => {
    if (
      !run ||
      !isLatestRun ||
      status !== "failed" ||
      commandLockRef.current
    ) {
      return;
    }
    const commandToken = beginRunCommand("retry", run.id);
    if (commandToken === null) {
      return;
    }
    setRetryError(null);
    const requestId =
      retryRequestRef.current?.runId === run.id
        ? retryRequestRef.current.requestId
        : newRequestId();
    retryRequestRef.current = { requestId, runId: run.id };
    try {
      const nextRun = await retryRun(sessionId, run.id, requestId);
      retryRequestRef.current = null;
      setRetryError(null);
      setRetryOpen(false);
      setCommandError(null);
      clearSessionHandoff(sessionId);
      if (!applyRunResult(nextRun, commandToken)) {
        // 投影被旧查询守卫拒绝时，释放旧选中 Run，由最新 Run 查询接续。
        setSelectedRunId(null);
      }
    } catch (error) {
      if (isCurrentRunCommand(commandToken)) {
        setRetryError(chatErrorMessage(error));
        flushReconcile();
      }
    } finally {
      endRunCommand(commandToken);
    }
  }, [
    applyRunResult,
    beginRunCommand,
    clearSessionHandoff,
    endRunCommand,
    flushReconcile,
    isCurrentRunCommand,
    isLatestRun,
    run,
    sessionId,
    status,
  ]);

  // 仍有上传中或上传失败的附件时阻止提交，避免静默漏掉用户选择的资料。
  const canSendWithoutDraft =
    !commandPending &&
    !hasBlockingUploads &&
    handoffUploads.length === 0 &&
    (isTargetedSubagentAnswer ||
      (isWaitingInput && Boolean(run?.pending_input)) ||
      (!isActive && Boolean(run || latestRunsIsSuccess)));
  const streamNotice = streamMatchesRun ? stream.state.notice : null;
  const transientUnfinished =
    Boolean(transientText) && (status === "failed" || status === "cancelled");

  const currentRunModelLabel =
    run?.model_snapshot?.scenario_label?.trim() ||
    run?.model_snapshot?.name?.trim() ||
    null;

  return {
    activeSubagentInput,
    approvalErrors,
    autoScrollRequest,
    busyApprovalIds,
    busySubagentApprovalKeys,
    canSendWithoutDraft,
    closeRetryDialog,
    commandError,
    commandNotice,
    commandPending,
    currentRunId,
    currentRunModelLabel,
    handleApproval,
    handleCancel,
    handleLoadMoreMessages,
    handleRetry,
    handleRetryMessages,
    handleSubagentApproval,
    handleSubmit,
    handoffUploads,
    initialDraft: initialHandoff?.draft ?? "",
    isActive,
    isLatestRun,
    isTargetedSubagentAnswer,
    isWaitingApproval,
    isWaitingInput,
    isWaitingSubagents,
    knowledgeBaseIds,
    latestRunsIsError,
    liveAnnouncement,
    messages,
    messagesQuery,
    modelProfileId,
    openRetryDialog,
    pendingSubagentApprovals,
    pendingSubagentInputs,
    questionText,
    refetchLatestRuns,
    refetchRunDetails,
    retryError,
    retryOpen,
    retrying,
    retryTerminalReconcile,
    run,
    runDetailsIsError,
    selectedFileIds,
    selectedSkillIds,
    sending,
    setHasBlockingUploads,
    setHandoffUploads,
    setKnowledgeBaseIds,
    setModelProfileId,
    setSelectedFileIds,
    setSelectedSkillIds,
    setSelectedSubagentInputKey,
    status,
    stopping,
    streamNotice,
    subagentApprovalErrors,
    subagentTasks,
    terminalMessageFailure,
    transientText,
    transientUnfinished,
  };
}
