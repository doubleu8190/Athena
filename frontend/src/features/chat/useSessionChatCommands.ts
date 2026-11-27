import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  useCallback,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from "react";

import type { PendingSessionUpload } from "../files/model";
import {
  answerRunInput,
  cancelRun,
  decideRunToolApproval,
  retryRun,
  submitRun,
} from "./api";
import type { ChatAutoScrollRequest } from "./ChatMessageList";
import { useDraftChat } from "./DraftChatContext";
import { chatErrorMessage, requestIdCanBeReused } from "./errorMessages";
import {
  runResultIsStale,
  type ConversationMessage,
  type PendingSubagentApproval,
  type PendingSubagentInput,
  type Run,
  type RunListResponse,
  type RunPendingApproval,
  type RunStatus,
} from "./model";
import { chatKeys } from "./queryKeys";
import { newRequestId } from "../../shared/lib/requestId";
import { subagentApprovalKey } from "./SubagentHitlPanels";

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

type ApprovalRequestCache = Record<
  string,
  { decision: "approve" | "reject"; requestId: string }
>;

export interface RunSnapshotRefValue {
  run: Run | null;
  status: RunStatus | null;
  streamRunId: string | null;
  streamStatus: RunStatus | null;
}

export interface UseSessionChatCommandsOptions {
  activeSubagentInput: PendingSubagentInput | null;
  currentRunId: string | null;
  flushReconcile: () => void;
  isActive: boolean;
  isLatestRun: boolean;
  isTargetedSubagentAnswer: boolean;
  isWaitingInput: boolean;
  latestRunsIsSuccess: boolean;
  messages: ConversationMessage[];
  run: Run | null;
  runSnapshotRef: RefObject<RunSnapshotRefValue>;
  sessionId: string;
  setSelectedRunId: Dispatch<SetStateAction<string | null>>;
  status: RunStatus | null;
  syncStreamStatus: (runId: string, status: RunStatus) => void;
}

/**
 * 同步补丁单个 Run 在详情缓存（chatKeys.run）与最新列表缓存（chatKeys.latestRun）中的投影，
 * 供 SSE 快照投影与子智能体 HITL 乐观更新共用，避免两处缓存口径漂移。
 */
export function patchCachedRunProjection(
  queryClient: QueryClient,
  sessionId: string,
  runId: string,
  updater: (existing: Run) => Run,
): void {
  queryClient.setQueryData<Run>(chatKeys.run(sessionId, runId), (existing) =>
    existing ? updater(existing) : existing,
  );
  queryClient.setQueryData<RunListResponse>(
    chatKeys.latestRun(sessionId),
    (existing) =>
      existing
        ? {
            ...existing,
            items: existing.items.map((item) =>
              item.id === runId ? updater(item) : item,
            ),
          }
        : existing,
  );
}

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

interface ComposerFingerprintCandidate {
  fileIds: string[];
  inputRequestId?: string;
  isAnswering: boolean;
  knowledgeBaseIds: string[] | null;
  modelProfileId: string | null;
  skillIds: string[];
  text: string;
}

function canReuseFailedComposerCommand(
  failedCommand: FailedComposerCommand | null,
  candidate: ComposerFingerprintCandidate,
): failedCommand is FailedComposerCommand {
  if (!failedCommand || !failedCommand.reuseRequestId) {
    return false;
  }
  return (
    failedCommand.text === candidate.text &&
    failedCommand.fileIds.join("\u0000") ===
      candidate.fileIds.join("\u0000") &&
    failedCommand.skillIds.join("\u0000") ===
      candidate.skillIds.join("\u0000") &&
    knowledgeScopeEqual(
      failedCommand.knowledgeBaseIds,
      candidate.knowledgeBaseIds,
    ) &&
    (failedCommand.modelProfileId ?? null) ===
      (candidate.modelProfileId ?? null) &&
    (candidate.isAnswering
      ? failedCommand.kind === "answer" &&
        failedCommand.inputRequestId === candidate.inputRequestId
      : failedCommand.kind === "submit")
  );
}

/** 相同决策重试时复用原 request_id，变更审批决策时签发全新幂等键。 */
function resolveApprovalRequestId(
  cache: ApprovalRequestCache,
  key: string,
  decision: "approve" | "reject",
): string {
  const previous = cache[key];
  const requestId =
    previous?.decision === decision ? previous.requestId : newRequestId();
  cache[key] = { decision, requestId };
  return requestId;
}

function selectNewestCachedRun(candidates: readonly Run[]): Run | null {
  return candidates.reduce<Run | null>((current, candidate) => {
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
}

/**
 * 会话工作台命令与审批子 Hook：
 * 集中封装共享命令锁、提交/回答幂等指纹缓存（failedCommand）、主任务工具审批、
 * 子智能体并行审批与定向回答乐观缓存补丁、失败重试弹窗及草稿交接输入器状态。
 */
export function useSessionChatCommands({
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
}: UseSessionChatCommandsOptions) {
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
  const [busySubagentApprovalKeys, setBusySubagentApprovalKeys] = useState<
    ReadonlySet<string>
  >(new Set());
  const [subagentApprovalErrors, setSubagentApprovalErrors] = useState<
    Record<string, string>
  >({});
  const [retryOpen, setRetryOpen] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [autoScrollRequest, setAutoScrollRequest] =
    useState<ChatAutoScrollRequest | null>(null);

  const commandLockRef = useRef<ActiveRunCommand | null>(null);
  const commandTokenRef = useRef(0);
  const approvalRequestsRef = useRef<ApprovalRequestCache>({});
  const busySubagentApprovalKeysRef = useRef<Set<string>>(new Set());
  const subagentApprovalRequestsRef = useRef<ApprovalRequestCache>({});
  const retryRequestRef = useRef<{ requestId: string; runId: string } | null>(
    null,
  );

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
        liveState &&
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
        liveState?.run?.id === nextRun.id ? liveState.run : null,
      ].filter((item): item is Run => Boolean(item));
      const currentRun = selectNewestCachedRun(cachedRuns);
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
      runSnapshotRef,
      sessionId,
      setSelectedRunId,
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
      const requestId = canReuseFailedComposerCommand(failedCommand, {
        fileIds: submittedFileIds,
        inputRequestId,
        isAnswering,
        knowledgeBaseIds: submittedKnowledgeBaseIds,
        modelProfileId: submittedModelProfileId,
        skillIds: submittedSkillIds,
        text,
      })
        ? failedCommand.requestId
        : newRequestId();

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
                    item.input_request_id ===
                    activeSubagentInput.input_request_id
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
            patchCachedRunProjection(
              queryClient,
              sessionId,
              currentRunId,
              (existing) => ({
                ...existing,
                pending_subagent_inputs: (
                  existing.pending_subagent_inputs ?? []
                ).filter(
                  (item) =>
                    item.input_request_id !==
                    activeSubagentInput.input_request_id,
                ),
              }),
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
      const requestId = resolveApprovalRequestId(
        approvalRequestsRef.current,
        approval.call_id,
        decision,
      );
      setApprovalErrors((current) => {
        if (!(approval.call_id in current)) {
          return current;
        }
        const next = { ...current };
        delete next[approval.call_id];
        return next;
      });
      setBusyApprovalIds((current) => new Set([...current, approval.call_id]));
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
          if (!(approval.call_id in current)) {
            return current;
          }
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
      const requestId = resolveApprovalRequestId(
        subagentApprovalRequestsRef.current,
        key,
        decision,
      );
      setSubagentApprovalErrors((current) => {
        if (!(key in current)) {
          return current;
        }
        const next = { ...current };
        delete next[key];
        return next;
      });
      setBusySubagentApprovalKeys((current) => new Set([...current, key]));
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
          patchCachedRunProjection(
            queryClient,
            sessionId,
            currentRunId,
            (existing) => ({
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
            }),
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
    setSelectedRunId,
    status,
  ]);

  const sending = activeCommand === "submit" || activeCommand === "answer";
  const stopping = activeCommand === "cancel";
  const retrying = activeCommand === "retry";
  const commandPending = activeCommand !== null;
  // 仍有上传中或上传失败的附件时阻止提交，避免静默漏掉用户选择的资料。
  const canSendWithoutDraft =
    !commandPending &&
    !hasBlockingUploads &&
    handoffUploads.length === 0 &&
    (isTargetedSubagentAnswer ||
      (isWaitingInput && Boolean(run?.pending_input)) ||
      (!isActive && Boolean(run || latestRunsIsSuccess)));

  return {
    approvalErrors,
    autoScrollRequest,
    busyApprovalIds,
    busySubagentApprovalKeys,
    canSendWithoutDraft,
    closeRetryDialog,
    commandError,
    commandNotice,
    commandPending,
    handleApproval,
    handleCancel,
    handleRetry,
    handleSubagentApproval,
    handleSubmit,
    handoffUploads,
    initialDraft: initialHandoff?.draft ?? "",
    knowledgeBaseIds,
    modelProfileId,
    openRetryDialog,
    retryError,
    retryOpen,
    retrying,
    selectedFileIds,
    selectedSkillIds,
    sending,
    setHasBlockingUploads,
    setHandoffUploads,
    setKnowledgeBaseIds,
    setModelProfileId,
    setSelectedFileIds,
    setSelectedSkillIds,
    stopping,
    subagentApprovalErrors,
  };
}
