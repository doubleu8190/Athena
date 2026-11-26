import { useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { sessionFileKeys } from "../files/queryKeys";
import { sessionKeys } from "../sessions/queryKeys";
import { findQuestionText } from "./ChatMessageList";
import { useLatestRun, useRunDetails, useSessionMessages } from "./hooks";
import {
  isActiveRunStatus,
  isTerminalRunStatus,
  mergeConversationMessages,
  selectRunProjection,
  shouldApplyStreamSnapshot,
  type ConversationMessage,
  type ConversationMessagePage,
  type Run,
  type RunListResponse,
  type RunSnapshotPayload,
  type RunStatus,
} from "./model";
import { chatKeys } from "./queryKeys";
import {
  completedEventAssistantMessageId,
  failedEventReportMessageId,
  fetchTerminalMessagesFallback,
  hasTerminalTargetMessage,
  projectRunStatus,
  resolveSelfContainedTerminalSnapshot,
  toConversationMessageFromSnapshot,
  type TerminalTargetMatcher,
} from "./runStream";
import { subagentInputKey } from "./SubagentHitlPanels";
import {
  useRunStream,
  type RunStreamReconcileContext,
} from "./useRunStream";
import {
  patchCachedRunProjection,
  useSessionChatCommands,
  type RunSnapshotRefValue,
} from "./useSessionChatCommands";

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

// 收到终态 run_event 预告后等待紧随其后的自包含终态 snapshot 的保护超时；
// 后端通常在同一循环内连续 yield，若流先 EOF/error 则立即接管，超时后关闭连接走单次有界兜底。
export const TERMINAL_SNAPSHOT_GRACE_MS = 300;

// 仅推进任务状态、工具调用进度、审批中间态、子智能体状态或上下文压缩的事件不会新增会话消息；
// completed/failed 终态消息由自包含终态快照或单次有界兜底独占负责，同样避免节流窗口触发重复消息页重拉。
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
  "completed",
  "failed",
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

function flattenCachedMessages(
  data: MessageInfiniteData | undefined,
): ConversationMessage[] {
  if (!data) {
    return [];
  }
  return data.pages.flatMap((page) => page.items);
}

function messageCarriesFileArtifact(message: ConversationMessage): boolean {
  return message.parts.some(
    (part) =>
      part.type === "file_ref" ||
      (part.type === "tool_result" && part.artifacts.length > 0),
  );
}

/**
 * 将自包含终态快照或单次有界兜底获取的消息按 id/sequence 幂等合并进 TanStack Query 缓存：
 * - 已存在的同 id / 同 sequence 消息就地覆盖；
 * - 新增消息写入最新页（pages[0]）并保持既有翻页游标，避免破坏向上加载历史状态。
 */
export function upsertMessagesIntoInfiniteData(
  current: MessageInfiniteData | undefined,
  incoming: readonly ConversationMessage[],
  fallbackNextBeforeSequence: number | null = null,
): MessageInfiniteData | undefined {
  if (incoming.length === 0) {
    return current;
  }
  const deduplicatedBySequence = new Map<number, ConversationMessage>();
  const incomingById = new Map<string, ConversationMessage>();
  for (const message of incoming) {
    deduplicatedBySequence.set(message.sequence, message);
    incomingById.set(message.id, message);
  }
  const normalizedIncoming = Array.from(deduplicatedBySequence.values()).sort(
    (left, right) => left.sequence - right.sequence,
  );
  if (!current || current.pages.length === 0) {
    return {
      pageParams: [undefined],
      pages: [
        {
          items: normalizedIncoming,
          next_before_sequence: fallbackNextBeforeSequence,
        },
      ],
    };
  }
  const matchedIds = new Set<string>();
  const matchedSequences = new Set<number>();
  const updatedPages = current.pages.map((page) => ({
    ...page,
    items: page.items.map((existing) => {
      const replacement =
        incomingById.get(existing.id) ??
        deduplicatedBySequence.get(existing.sequence);
      if (!replacement) {
        return existing;
      }
      matchedIds.add(replacement.id);
      matchedSequences.add(replacement.sequence);
      return replacement;
    }),
  }));
  const additions = normalizedIncoming.filter(
    (message) =>
      !matchedIds.has(message.id) && !matchedSequences.has(message.sequence),
  );
  if (additions.length > 0) {
    const latestPage = updatedPages[0];
    updatedPages[0] = {
      ...latestPage,
      items: [...latestPage.items, ...additions].sort(
        (left, right) => left.sequence - right.sequence,
      ),
      // 仅在空占位页首次填充时采用 fallbackNextBeforeSequence；
      // 已有消息（尤其是 next_before_sequence === null 已触顶历史页）严禁被裁剪快照游标覆写。
      next_before_sequence:
        latestPage.items.length === 0
          ? fallbackNextBeforeSequence
          : latestPage.next_before_sequence,
    };
  }
  return {
    ...current,
    pages: updatedPages,
  };
}

export interface UseSessionChatControllerOptions {
  sessionId: string;
  /** 服务端会话标题：null 表示尚无标题，undefined 表示会话详情仍在加载中。 */
  sessionTitle: string | null | undefined;
}

/**
 * 会话工作台控制器 Hook：
 * 统一封装 SSE 实时流连接与单调快照投影、自包含终态快照内联落盘、单次有界终态兜底刷新、
 * 500ms 精细化节流对账、首轮异步自动标题静默刷新、5 节点 aria-live 无障碍通告，
 * 并将用户命令、幂等指纹与主/子智能体审批委托给 useSessionChatCommands。
 */
export function useSessionChatController({
  sessionId,
  sessionTitle,
}: UseSessionChatControllerOptions) {
  const queryClient = useQueryClient();

  const [selectedSubagentInputKey, setSelectedSubagentInputKey] = useState<
    string | null
  >(null);
  const [terminalMessageFailure, setTerminalMessageFailure] =
    useState<TerminalMessageFailure | null>(null);
  const [terminalMessageRetry, setTerminalMessageRetry] = useState(0);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [liveAnnouncement, setLiveAnnouncement] = useState("");

  const runSnapshotRef = useRef<RunSnapshotRefValue>({
    run: null,
    status: null,
    streamRunId: null,
    streamStatus: null,
  });
  const settledTerminalRunsRef = useRef(new Set<string>());
  // 记录每个终态 Run 已执行过单次有界兜底的 retry 序号，防止后台 Run 查询刷新重复触发自动兜底，
  // 同时允许用户点击手动重试按钮（terminalMessageRetry 递增）发起下一次单次有界拉取。
  const attemptedTerminalFallbackRef = useRef(new Map<string, number>());
  const settleStreamTerminalRef = useRef<
    (runId: string, status: RunStatus) => void
  >(() => {});
  // 用 ref 读取最新瞬时文本，避免终态关流 flush 最后一帧草稿时误取消或重复触发终态单次兜底。
  const transientTextRef = useRef("");
  const reconcileTimerRef = useRef<number | null>(null);
  // 同一节流窗口内若先收到纯进度事件、后收到需刷新消息的事件，合并保留消息失效意图不丢失。
  const pendingMessagesReconcileRef = useRef(false);
  const suppressNextScheduledReconcileRef = useRef(false);
  // 记录运行中 live snapshot 已处理过的新消息 ID，防止周期快照重复失效或重复合并。
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
    isFetchedAfterMount: messagesFetchedAfterMount,
    isPending: messagesPending,
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

  // 命令纠偏以及等待态/取消态快照需要零延迟全量刷新（含消息流），先清掉尚未触发的节流定时器避免重复请求。
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

      // SSE 快照包含子任务、并发 HITL 及终态消息 ID 投影；写入前校验单调性，禁止终态、取消态或旧序号回退覆盖。
      patchCachedRunProjection(queryClient, sessionId, currentRunId, (existing) =>
        shouldApplyStreamSnapshot(existing, snapshot)
          ? {
              ...existing,
              ...(snapshot.assistant_message_id !== undefined
                ? { assistant_message_id: snapshot.assistant_message_id }
                : {}),
              event_sequence: snapshot.event_sequence,
              pending_subagent_approvals:
                snapshot.pending_subagent_approvals ?? [],
              pending_subagent_inputs: snapshot.pending_subagent_inputs ?? [],
              report_message_id: snapshot.report_message_id,
              status: snapshot.status,
              subagent_tasks: snapshot.subagent_tasks ?? [],
            }
          : existing,
      );

      // 等待人工输入/审批或取消态快照随后会关闭 SSE，立即刷新完整对账并跳过重复节流回调。
      if (
        snapshot.status === "waiting_for_input" ||
        snapshot.status === "waiting_for_approval" ||
        snapshot.status === "cancelled"
      ) {
        suppressNextScheduledReconcileRef.current = true;
        flushReconcile();
        return;
      }

      // completed / failed 终态快照优先直接消费内联完整消息（含 parts），零额外 /events 或 /messages 往返；
      // 仅当目标消息因 >512KB 被裁剪或旧快照缺 parts 时，交由终态 effect 执行单次有界 GET /messages 兜底。
      if (snapshot.status === "completed" || snapshot.status === "failed") {
        suppressNextScheduledReconcileRef.current = true;
        clearReconcileTimer();
        const statusConsistent =
          !existingRun ||
          !isTerminalRunStatus(existingRun.status) ||
          existingRun.status === snapshot.status;
        if (statusConsistent) {
          const resolved = resolveSelfContainedTerminalSnapshot(
            snapshot,
            sessionId,
            currentRunId,
            existingRun?.trigger_message_id ?? null,
          );
          if (resolved) {
            queryClient.setQueryData<MessageInfiniteData>(
              chatKeys.messages(sessionId),
              (current) =>
                upsertMessagesIntoInfiniteData(
                  current,
                  resolved.messages,
                  snapshot.messages_before_sequence,
                ),
            );
            settledTerminalRunsRef.current.add(
              `${currentRunId}:${snapshot.status}`,
            );
            setTerminalMessageFailure(null);
            settleStreamTerminalRef.current(currentRunId, snapshot.status);
          } else {
            // 未命中完整终态目标时，仍把快照内自带完整 parts 的已有消息合入缓存，保留 transientText 等待兜底。
            const completeSnapshotMessages = snapshot.messages
              .map((message) =>
                toConversationMessageFromSnapshot(
                  message,
                  sessionId,
                  currentRunId,
                ),
              )
              .filter(
                (message): message is ConversationMessage => message !== null,
              );
            if (completeSnapshotMessages.length > 0) {
              queryClient.setQueryData<MessageInfiniteData>(
                chatKeys.messages(sessionId),
                (current) =>
                  upsertMessagesIntoInfiniteData(
                    current,
                    completeSnapshotMessages,
                    snapshot.messages_before_sequence,
                  ),
              );
            }
          }
        }
        // 仅刷新 Run 详情与工具调用元数据（usage / finished_at / toolCalls），避免重复触发 /messages 查询。
        invalidateRunQueries({ includeMessages: false });
        return;
      }

      // 运行中快照出现未缓存新消息：携带完整 parts 时直接合入缓存，旧版纯文本快照则精准补拉消息与文件列表。
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
      const selfContainedUnseen: ConversationMessage[] = [];
      const legacyUnseenIds: string[] = [];
      for (const message of snapshot.messages) {
        if (
          cachedMessageIds.has(message.id) ||
          requestedSnapshotMessageIdsRef.current.has(message.id)
        ) {
          continue;
        }
        const converted = toConversationMessageFromSnapshot(
          message,
          sessionId,
          currentRunId,
        );
        if (converted) {
          selfContainedUnseen.push(converted);
        } else {
          legacyUnseenIds.push(message.id);
        }
      }
      if (selfContainedUnseen.length > 0) {
        for (const message of selfContainedUnseen) {
          requestedSnapshotMessageIdsRef.current.add(message.id);
        }
        queryClient.setQueryData<MessageInfiniteData>(
          chatKeys.messages(sessionId),
          (current) =>
            upsertMessagesIntoInfiniteData(
              current,
              selfContainedUnseen,
              snapshot.messages_before_sequence,
            ),
        );
        if (selfContainedUnseen.some(messageCarriesFileArtifact)) {
          void queryClient.invalidateQueries({
            queryKey: sessionFileKeys.list(sessionId),
          });
        }
      }
      if (legacyUnseenIds.length === 0) {
        return;
      }
      for (const messageId of legacyUnseenIds) {
        requestedSnapshotMessageIdsRef.current.add(messageId);
      }
      void queryClient.invalidateQueries({
        queryKey: chatKeys.messages(sessionId),
      });
      void queryClient.invalidateQueries({
        queryKey: sessionFileKeys.list(sessionId),
      });
    },
    [
      clearReconcileTimer,
      currentRunId,
      flushReconcile,
      invalidateRunQueries,
      queryClient,
      sessionId,
    ],
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
  settleStreamTerminalRef.current = settleStreamTerminal;

  const streamMatchesRun = stream.state.runId === currentRunId;
  const streamEvents = stream.state.events;
  const streamAssistantMessageId = streamMatchesRun
    ? stream.state.assistantMessageId
    : null;
  const streamReportMessageId = streamMatchesRun
    ? stream.state.reportMessageId
    : null;
  const transientText = streamMatchesRun ? stream.state.transientText : "";
  transientTextRef.current = transientText;
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

  // 后端 RunStreamService 固定先 yield 终态 run_event 再 yield 自包含终态 snapshot；
  // 提前收敛为单一布尔值，避免非等待态下 connection 从 open -> closed 引发终态 effect 重复执行。
  const awaitingTerminalSnapshot = Boolean(
    currentRunId &&
      status &&
      isTerminalRunStatus(status) &&
      streamMatchesRun &&
      !stream.state.terminalSnapshotReceived &&
      stream.state.connection === "open" &&
      !isTerminalRunStatus(run?.status),
  );

  // 终态收敛与单次有界兜底：
  // 1) 若仅收到终态 run_event 预告且 SSE 仍打开、REST 尚未独立确认终态，暂缓关流等待下一帧自包含终态 snapshot；
  // 2) 自包含终态快照已在 handleStreamSnapshot 内联落盘并标记 settled，此处直接跳过 HTTP 拉取；
  // 3) 普通无报告 failed 与 cancelled 保留瞬时未完成文本，不发起 /events 或 /messages 查询；
  // 4) 仅在 >512KB 裁剪、旧快照缺 parts、无 snapshot 断流或缓存缺口时执行单次有界 GET /messages?limit=20；
  //    未命中时保留 transientText 并暴露 not_found 手动重试入口，绝不陷入多页翻页循环。
  useEffect(() => {
    if (!currentRunId || !status || !isTerminalRunStatus(status)) {
      return;
    }
    const terminalRunId = currentRunId;
    const terminalStatus = status;

    if (awaitingTerminalSnapshot) {
      const graceTimer = window.setTimeout(() => {
        closeStreamTerminal(terminalRunId, terminalStatus);
      }, TERMINAL_SNAPSHOT_GRACE_MS);
      return () => {
        window.clearTimeout(graceTimer);
      };
    }

    closeStreamTerminal(terminalRunId, terminalStatus);
    if (reconcileTimerRef.current !== null) {
      clearReconcileTimer();
      invalidateRunQueries({ includeMessages: false });
    }
    // 刷新和终态恢复都重新读取服务端文件发布记录，确保产物立即同步至文件抽屉与附件菜单。
    void queryClient.invalidateQueries({
      queryKey: sessionFileKeys.list(sessionId),
    });
    // 取消终态没有权威回答；旧工具/文件交付消息不能用于清除临时文本。
    if (terminalStatus === "cancelled") {
      return;
    }
    const projectedReportMessageId =
      terminalStatus === "failed"
        ? (run?.report_message_id ??
          streamReportMessageId ??
          failedEventReportMessageId(streamEvents, terminalRunId))
        : null;
    // 普通失败无安全停止报告时保留未完成草稿提示，不再扫描 /events 或 /messages。
    if (terminalStatus === "failed" && !projectedReportMessageId) {
      return;
    }

    const terminalKey = `${terminalRunId}:${terminalStatus}`;
    if (settledTerminalRunsRef.current.has(terminalKey)) {
      return;
    }

    const projectedAssistantMessageId =
      terminalStatus === "completed"
        ? (run?.assistant_message_id ??
          streamAssistantMessageId ??
          completedEventAssistantMessageId(streamEvents, terminalRunId))
        : null;
    const matcher: TerminalTargetMatcher = {
      assistantMessageId: projectedAssistantMessageId,
      reportMessageId: projectedReportMessageId,
      runId: terminalRunId,
      status: terminalStatus,
      triggerMessageId: run?.trigger_message_id ?? null,
    };

    const cachedMessages = flattenCachedMessages(
      queryClient.getQueryData<MessageInfiniteData>(
        chatKeys.messages(sessionId),
      ),
    );
    const hasExactTargetId = Boolean(
      terminalStatus === "completed"
        ? projectedAssistantMessageId
        : projectedReportMessageId,
    );
    const wasObservedLive =
      observedActiveRunIdsRef.current.has(terminalRunId) || streamMatchesRun;
    const cachedHasTarget = hasTerminalTargetMessage(cachedMessages, matcher);

    // 已明确知道权威消息 ID 且当前消息缓存已命中，直接收敛零额外请求。
    if (hasExactTargetId && cachedHasTarget) {
      settledTerminalRunsRef.current.add(terminalKey);
      setTerminalMessageFailure(null);
      settleStreamTerminal(terminalRunId, terminalStatus);
      return;
    }

    // 页面刷新直达已终态 Run（无活跃 SSE 流、无未落盘草稿且非手动重试）：复用首屏 useSessionMessages，不重复请求。
    if (
      !wasObservedLive &&
      !transientTextRef.current &&
      terminalMessageRetry === 0
    ) {
      if (messagesPending) {
        return;
      }
      if (cachedHasTarget) {
        settledTerminalRunsRef.current.add(terminalKey);
        setTerminalMessageFailure(null);
        settleStreamTerminal(terminalRunId, terminalStatus);
        return;
      }
      if (messagesFetchedAfterMount) {
        if (hasExactTargetId) {
          setTerminalMessageFailure({
            kind: "not_found",
            runId: terminalRunId,
          });
        } else {
          settledTerminalRunsRef.current.add(terminalKey);
        }
        void queryClient.invalidateQueries({
          queryKey: chatKeys.messages(sessionId),
          refetchType: "none",
        });
        return;
      }
    }

    // 当前 retry 序号已触发过单次有界兜底时不再因后台 Run 详情刷新自动重发，等待用户显式点击重试。
    const attemptedFallbacks = attemptedTerminalFallbackRef.current;
    if (attemptedFallbacks.get(terminalKey) === terminalMessageRetry) {
      return;
    }
    attemptedFallbacks.set(terminalKey, terminalMessageRetry);

    let active = true;
    let completed = false;
    setTerminalMessageFailure(null);
    void fetchTerminalMessagesFallback(sessionId, matcher)
      .then((result) => {
        completed = true;
        if (!active) {
          return;
        }
        queryClient.setQueryData<MessageInfiniteData>(
          chatKeys.messages(sessionId),
          (current) =>
            upsertMessagesIntoInfiniteData(
              current,
              result.page.items,
              result.page.next_before_sequence,
            ),
        );
        if (result.targetFound) {
          settledTerminalRunsRef.current.add(terminalKey);
          setTerminalMessageFailure(null);
          settleStreamTerminal(terminalRunId, terminalStatus);
          return;
        }
        // 单次有界兜底未命中目标消息：保留 transientText，暴露 not_found 提示与手动重试入口，绝不自动翻页循环。
        setTerminalMessageFailure({
          kind: "not_found",
          runId: terminalRunId,
        });
        void queryClient.invalidateQueries({
          queryKey: chatKeys.messages(sessionId),
          refetchType: "none",
        });
      })
      .catch(() => {
        completed = true;
        if (active) {
          setTerminalMessageFailure({
            kind: "sync_failed",
            runId: terminalRunId,
          });
        }
      });
    return () => {
      active = false;
      // 若在途兜底因 StrictMode 双挂载或依赖重跑被提前取消，回滚登记确保后续挂载可正常完成单次兜底。
      if (
        !completed &&
        attemptedFallbacks.get(terminalKey) === terminalMessageRetry
      ) {
        attemptedFallbacks.delete(terminalKey);
      }
    };
  }, [
    awaitingTerminalSnapshot,
    clearReconcileTimer,
    closeStreamTerminal,
    currentRunId,
    invalidateRunQueries,
    messagesFetchedAfterMount,
    messagesPending,
    queryClient,
    run?.assistant_message_id,
    run?.report_message_id,
    run?.trigger_message_id,
    sessionId,
    settleStreamTerminal,
    status,
    streamAssistantMessageId,
    streamEvents,
    streamMatchesRun,
    streamReportMessageId,
    terminalMessageRetry,
  ]);

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

  const commands = useSessionChatCommands({
    activeSubagentInput,
    currentRunId,
    flushReconcile,
    isActive,
    isLatestRun,
    isTargetedSubagentAnswer,
    isWaitingInput,
    latestRunsIsSuccess,
    messages,
    run,
    runSnapshotRef,
    sessionId,
    setSelectedRunId,
    status,
    syncStreamStatus,
  });

  const streamNotice = streamMatchesRun ? stream.state.notice : null;
  const transientUnfinished =
    Boolean(transientText) && (status === "failed" || status === "cancelled");
  const currentRunModelLabel =
    run?.model_snapshot?.scenario_label?.trim() ||
    run?.model_snapshot?.name?.trim() ||
    null;

  return {
    ...commands,
    activeSubagentInput,
    currentRunId,
    currentRunModelLabel,
    handleLoadMoreMessages,
    handleRetryMessages,
    isActive,
    isLatestRun,
    isTargetedSubagentAnswer,
    isWaitingApproval,
    isWaitingInput,
    isWaitingSubagents,
    latestRunsIsError,
    liveAnnouncement,
    messages,
    messagesQuery,
    pendingSubagentApprovals,
    pendingSubagentInputs,
    questionText,
    refetchLatestRuns,
    refetchRunDetails,
    retryTerminalReconcile,
    run,
    runDetailsIsError,
    setSelectedSubagentInputKey,
    status,
    streamNotice,
    subagentTasks,
    terminalMessageFailure,
    transientText,
    transientUnfinished,
  };
}
