import {
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link, useParams } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { Modal } from "../../shared/ui/Modal";
import { useProjectDirectory } from "../projects/hooks";
import { SessionFilesPanel } from "../files/SessionFilesPanel";
import type { PendingSessionUpload } from "../files/model";
import { sessionFileKeys } from "../files/queryKeys";
import { SkillCatalogPicker } from "../skills/SkillCatalogPicker";
import {
  isSessionNotFound,
  sessionErrorMessage,
} from "../sessions/errorMessages";
import { useSession } from "../sessions/hooks";
import { sessionDisplayTitle } from "../sessions/model";
import {
  answerRunInput,
  cancelRun,
  decideRunToolApproval,
  retryRun,
  submitRun,
} from "./api";
import {
  ChatMessageList,
  findQuestionText,
} from "./ChatMessageList";
import { useDraftChat } from "./DraftChatContext";
import {
  chatErrorMessage,
  requestIdCanBeReused,
} from "./errorMessages";
import {
  useLatestRun,
  useRunDetails,
  useSessionMessages,
} from "./hooks";
import { MarkdownContent } from "./MarkdownContent";
import {
  isActiveRunStatus,
  isTerminalRunStatus,
  mergeConversationMessages,
  runResultIsStale,
  selectRunProjection,
  type ConversationMessagePage,
  type Run,
  type RunListResponse,
  type RunPendingApproval,
  type RunStatus,
} from "./model";
import { chatKeys } from "./queryKeys";
import { newRequestId } from "./requestId";
import {
  projectRunStatus,
  reconcileCompletedRun,
} from "./runStream";
import { RunExecutionTrace } from "./RunExecutionTrace";
import { RunStatusBadge } from "./RunStatusBadge";
import { ToolApprovalPanel } from "./ToolApprovalPanel";
import { useRunStream } from "./useRunStream";

interface FailedComposerCommand {
  fileIds: string[];
  inputRequestId?: string;
  kind: "answer" | "submit";
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

interface TerminalMessageFailure {
  kind: "not_found" | "sync_failed";
  runId: string;
}

type MessageInfiniteData = InfiniteData<
  ConversationMessagePage,
  number | undefined
>;

const RETRY_RISK_WARNING =
  "整个任务将从头执行，可能重复此前已成功的操作；原操作结果未知时也可能已生效。";

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
    pages: [
      ...current.pages,
      ...additions.map((item) => item.page),
    ],
  };
}

function ComposerHint({
  hasPendingUploads,
  status,
}: {
  hasPendingUploads: boolean;
  status: RunStatus | null;
}) {
  if (hasPendingUploads) {
    return (
      <span>
        <Icon name="upload" size={13} />
        请先在会话文件中上传或移除待上传文件，再提交消息或补充回答。
      </span>
    );
  }
  if (status === "queued") {
    return <span>任务已排队，重复提交已锁定；停止后以服务端状态为准。</span>;
  }
  if (status === "running") {
    return <span>任务运行中，新消息暂不可提交；已输入内容会保留。</span>;
  }
  if (status === "waiting_for_input") {
    return <span>请回答服务端当前提问；失败时输入和请求标识都会保留。</span>;
  }
  if (status === "waiting_for_approval") {
    return <span>请逐项处理真实工具审批，全部决定后 Run 会自动继续。</span>;
  }
  if (status === "cancelling") {
    return <span>正在等待服务端确认取消，不能提前视为已取消。</span>;
  }
  return (
    <span>
      <Icon name="shield" size={13} />
      每次提交使用独立请求标识；网络失败重试会复用同一标识。
    </span>
  );
}

function RunSummary({
  canRetry,
  commandPending,
  onOpenTrace,
  onRetry,
  run,
  status,
  traceOpen,
}: {
  canRetry: boolean;
  commandPending: boolean;
  onOpenTrace: () => void;
  onRetry: () => void;
  run: Run | null;
  status: RunStatus | null;
  traceOpen: boolean;
}) {
  if (!run || !status) {
    return (
      <div className="run-summary run-summary--empty">
        <div>
          <span className="run-summary-label">当前运行</span>
          <strong>尚未提交 Run</strong>
        </div>
        <span className="run-summary-meta">发送消息后可查看真实执行轨迹。</span>
      </div>
    );
  }

  const selectedSkills =
    run.skill_snapshot?.skills.filter((skill) =>
      run.skill_snapshot?.selected_skill_ids.includes(skill.skill_id),
    ) ?? [];

  return (
    <div className="run-summary">
      <div className="run-summary-primary">
        <RunStatusBadge status={status} />
        <span className="run-summary-id" title={run.id}>
          Run {run.id}
        </span>
      </div>
      <div className="run-summary-meta">
        <span>事件 #{run.event_sequence}</span>
        <span>执行代次 {run.execution_epoch}</span>
        {run.first_started_at ? (
          <span>开始 {formatDateTime(run.first_started_at)}</span>
        ) : null}
        {run.finished_at ? (
          <span>结束 {formatDateTime(run.finished_at)}</span>
        ) : null}
        {run.cancel_requested_at ? <span>已请求取消</span> : null}
      </div>
      {run.safe_error_code ? (
        <p className="run-summary-error">安全错误码：{run.safe_error_code}</p>
      ) : null}
      {run.skill_snapshot ? (
        <div className="run-skill-snapshot" aria-label="本次运行固定技能">
          <span>固定技能</span>
          {selectedSkills.length === 0 ? (
            <small>未手动指定，允许 Agent 按任务加载已启用技能</small>
          ) : (
            selectedSkills.map((skill) => (
              <span
                className="run-skill-snapshot-item"
                key={skill.skill_id}
                title={`${skill.name} · ${skill.version_label}`}
              >
                {skill.name} · {skill.version_label}
              </span>
            ))
          )}
        </div>
      ) : null}
      {run.retry_warning ? (
        <p
          aria-label="服务端重试警告"
          className="run-summary-warning"
          role="note"
        >
          {run.retry_warning}
        </p>
      ) : null}
      <div className="run-summary-actions">
        <button
          className="button-quiet"
          disabled={traceOpen}
          onClick={onOpenTrace}
          type="button"
        >
          <Icon name="tools" size={14} />
          {traceOpen ? "轨迹已展开" : "查看执行轨迹"}
        </button>
        {status === "failed" && canRetry ? (
          <button
            className="button-danger-outline"
            disabled={commandPending}
            onClick={onRetry}
            type="button"
          >
            重试失败任务
          </button>
        ) : null}
      </div>
    </div>
  );
}

function SessionConversation({ sessionId }: { sessionId: string }) {
  const queryClient = useQueryClient();
  const { clearSessionHandoff, getSessionHandoff } = useDraftChat();
  const initialHandoff = useMemo(
    () => getSessionHandoff(sessionId),
    [getSessionHandoff, sessionId],
  );
  const [draft, setDraft] = useState(initialHandoff?.draft ?? "");
  const [selectedSkillIds, setSelectedSkillIds] = useState<string[]>(
    initialHandoff?.selectedSkillIds ?? [],
  );
  const [selectedFileIds, setSelectedFileIds] = useState<string[]>(
    initialHandoff?.inputFileIds ?? [],
  );
  const [handoffUploads, setHandoffUploads] = useState<
    PendingSessionUpload[]
  >(initialHandoff?.pendingUploads ?? []);
  const [filePanelBusy, setFilePanelBusy] = useState(false);
  const [commandError, setCommandError] = useState<string | null>(
    initialHandoff?.error ?? null,
  );
  const [commandNotice, setCommandNotice] = useState<string | null>(null);
  const [failedCommand, setFailedCommand] =
    useState<FailedComposerCommand | null>(
      initialHandoff
        ? {
            kind: "submit",
            fileIds: initialHandoff.inputFileIds ?? [],
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
  const [approvalErrors, setApprovalErrors] = useState<
    Record<string, string>
  >({});
  const [retryOpen, setRetryOpen] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [terminalMessageFailure, setTerminalMessageFailure] =
    useState<TerminalMessageFailure | null>(null);
  const [terminalMessageRetry, setTerminalMessageRetry] = useState(0);
  const [traceOpen, setTraceOpen] = useState(
    () => typeof window === "undefined" || window.innerWidth > 920,
  );
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
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

  const messagesQuery = useSessionMessages(sessionId);
  const latestRuns = useLatestRun(sessionId);
  const latestRun = latestRuns.data?.items[0] ?? null;
  const currentRunId = selectedRunId ?? latestRun?.id ?? null;
  const runDetails = useRunDetails(sessionId, currentRunId);
  const run = selectRunProjection(
    currentRunId,
    latestRun,
    runDetails.data ?? null,
  );

  const reconcileRunState = useCallback(() => {
    void queryClient.invalidateQueries({
      queryKey: chatKeys.latestRun(sessionId),
    });
    void queryClient.invalidateQueries({
      queryKey: chatKeys.messages(sessionId),
    });
    if (currentRunId) {
      void queryClient.invalidateQueries({
        queryKey: chatKeys.run(sessionId, currentRunId),
      });
      void queryClient.invalidateQueries({
        queryKey: chatKeys.toolCalls(sessionId, currentRunId),
      });
    }
  }, [currentRunId, queryClient, sessionId]);

  const stream = useRunStream({
    onReconcile: reconcileRunState,
    runId: currentRunId,
    sessionId,
    status: run?.status ?? null,
  });
  const streamMatchesRun = stream.state.runId === currentRunId;
  const streamMessages = streamMatchesRun ? stream.state.messages : [];
  const transientText = streamMatchesRun
    ? stream.state.transientText
    : "";
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

  useEffect(() => {
    if (
      !currentRunId ||
      !status ||
      !isTerminalRunStatus(status)
    ) {
      return;
    }
    const terminalRunId = currentRunId;
    const terminalStatus = status;
    const triggerMessageId = run?.trigger_message_id;
    stream.closeTerminal(terminalRunId, terminalStatus);
    // 刷新和终态恢复都重新读取服务端文件发布记录，不能只依赖临时消息投影。
    void queryClient.invalidateQueries({
      queryKey: sessionFileKeys.list(sessionId),
    });
    // 失败和取消没有权威最终回答；旧工具/文件交付消息不能用于清除临时文本。
    if (terminalStatus === "failed" || terminalStatus === "cancelled") {
      return;
    }
    if (!triggerMessageId) {
      setTerminalMessageFailure({
        kind: "sync_failed",
        runId: terminalRunId,
      });
      return;
    }
    const terminalKey = terminalRunId + ":" + terminalStatus;
    if (settledTerminalRunsRef.current.has(terminalKey)) {
      return;
    }
    let active = true;
    setTerminalMessageFailure(null);
    void reconcileCompletedRun(
      sessionId,
      terminalRunId,
      triggerMessageId,
    )
      .then((result) => {
        if (result.kind === "not_found") {
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
        stream.settleTerminal(terminalRunId, terminalStatus);
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
    currentRunId,
    queryClient,
    run?.trigger_message_id,
    sessionId,
    status,
    stream.closeTerminal,
    stream.settleTerminal,
    terminalMessageRetry,
  ]);

  // 同一 Run 的所有用户命令共享一个显式锁；ref 在事件入口同步生效，
  // activeCommand 仅负责驱动按钮 disabled 与 aria-busy。
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
        streamMessages,
      ),
    [currentRunId, messagesQuery.data?.pages, streamMessages],
  );

  const questionText = useMemo(() => {
    const questionMessageId = run?.pending_input?.question_message_id;
    return questionMessageId
      ? findQuestionText(messages, questionMessageId)
      : null;
  }, [messages, run?.pending_input?.question_message_id]);

  const applyRunResult = useCallback(
    (nextRun: Run, commandToken: number): boolean => {
      // 结果只允许发起它的当前命令应用；共享锁之外再保留令牌守卫，
      // 防止异常调度或旧异步链覆盖后续命令状态。
      if (!isCurrentRunCommand(commandToken)) {
        reconcileRunState();
        return false;
      }
      const activeCommand = commandLockRef.current;
      if (
        activeCommand?.runId &&
        (activeCommand.kind === "answer" ||
          activeCommand.kind === "approval" ||
          activeCommand.kind === "cancel") &&
        activeCommand.runId !== nextRun.id
      ) {
        reconcileRunState();
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
        reconcileRunState();
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
        return candidate.updated_at > current.updated_at ? candidate : current;
      }, null);
      if (currentRun && runResultIsStale(nextRun, currentRun)) {
        reconcileRunState();
        return false;
      }

      setSelectedRunId(nextRun.id);
      stream.syncStatus(nextRun.id, nextRun.status);
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
      isCurrentRunCommand,
      queryClient,
      reconcileRunState,
      sessionId,
      stream.syncStatus,
    ],
  );

  const handleSubmit = async () => {
    const submittedDraft = draft;
    const text = submittedDraft.trim();
    if (!text || commandLockRef.current) {
      return;
    }
    if (handoffUploads.length > 0) {
      setCommandError(
        "请先上传或移除会话文件面板中的待上传文件，再提交消息或补充回答。",
      );
      return;
    }
    if (isWaitingInput && !run?.pending_input) {
      setCommandError("服务端尚未返回可回答的输入请求，正在同步状态。");
      reconcileRunState();
      return;
    }
    if (isActive && !isWaitingInput) {
      return;
    }
    if (!run && !latestRuns.isSuccess) {
      return;
    }

    const inputRequestId = isWaitingInput
      ? run?.pending_input?.id
      : undefined;
    const submittedSkillIds = isWaitingInput ? [] : [...selectedSkillIds];
    const submittedFileIds = [...selectedFileIds];
    const commandKind: RunCommandKind = isWaitingInput ? "answer" : "submit";
    const commandToken = beginRunCommand(commandKind, run?.id ?? null);
    if (commandToken === null) {
      return;
    }
    const canReuse =
      failedCommand?.reuseRequestId === true &&
      failedCommand?.text === text &&
      failedCommand.fileIds.join("\u0000") ===
        submittedFileIds.join("\u0000") &&
      failedCommand.skillIds.join("\u0000") ===
        submittedSkillIds.join("\u0000") &&
      (isWaitingInput
        ? failedCommand.kind === "answer" &&
          failedCommand.inputRequestId === inputRequestId
        : failedCommand?.kind === "submit");
    const requestId = canReuse ? failedCommand.requestId : newRequestId();

    setCommandError(null);
    try {
      const nextRun =
        isWaitingInput && run && inputRequestId
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
            );
      // HTTP 成功先确认命令结果；投影是否采纳只决定 Run 状态如何同步。
      applyRunResult(nextRun, commandToken);
      if (isCurrentRunCommand(commandToken)) {
        // 请求期间用户可能继续编辑；只清空仍与提交时完全一致的原草稿。
        setDraft((current) =>
          current === submittedDraft ? "" : current,
        );
        setFailedCommand(null);
        setSelectedFileIds([]);
        // 新 Run 与澄清回答都只消费已经提交的固定快照；交回控制权后不能把
        // 上一 Run 的手动选择静默带入后续任务。
        setSelectedSkillIds([]);
        setHandoffUploads([]);
        clearSessionHandoff(sessionId);
        setCommandNotice(
          isWaitingInput
            ? "补充回答已提交，正在按服务端最新运行状态同步。"
            : "消息已提交，正在按服务端最新运行状态同步。",
        );
      }
    } catch (error) {
      if (isCurrentRunCommand(commandToken)) {
        setFailedCommand({
          fileIds: submittedFileIds,
          inputRequestId,
          kind: isWaitingInput ? "answer" : "submit",
          requestId,
          reuseRequestId: requestIdCanBeReused(error),
          skillIds: submittedSkillIds,
          text,
        });
        setCommandNotice(null);
        setCommandError(chatErrorMessage(error));
        // 命令失败后重新读取最新 Run，避免继续以已失效的等待/活跃状态展示。
        reconcileRunState();
      }
    } finally {
      endRunCommand(commandToken);
    }
  };

  const handleCancel = async () => {
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
        reconcileRunState();
      }
    } finally {
      endRunCommand(commandToken);
    }
  };

  const handleApproval = async (
    approval: RunPendingApproval,
    decision: "approve" | "reject",
  ) => {
    if (!run || commandLockRef.current) {
      return;
    }
    const commandToken = beginRunCommand("approval", run.id);
    if (commandToken === null) {
      return;
    }
    // 审批响应包含整份 Run 投影；串行提交可避免较早响应迟到后覆盖
    // 最后一次决定产生的 queued 状态。
    const previous = approvalRequestsRef.current[approval.call_id];
    const requestId =
      previous?.decision === decision
        ? previous.requestId
        : newRequestId();
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
        run.id,
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
        reconcileRunState();
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
  };

  const handleRetry = async () => {
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
        reconcileRunState();
      }
    } finally {
      endRunCommand(commandToken);
    }
  };

  // 待上传文件还没有服务端 file ID，必须阻止 Run 以免静默漏掉用户选择。
  const hasPendingUploads = handoffUploads.length > 0;
  const canSubmit =
    !commandPending &&
    !filePanelBusy &&
    !hasPendingUploads &&
    Boolean(draft.trim()) &&
    ((isWaitingInput && Boolean(run?.pending_input)) ||
      (!isActive && Boolean(run || latestRuns.isSuccess)));
  const streamNotice = streamMatchesRun ? stream.state.notice : null;
  const transientUnfinished =
    Boolean(transientText) &&
    (status === "failed" || status === "cancelled");
  const serverRetryWarning = run?.retry_warning?.trim() ?? "";
  const distinctServerRetryWarning =
    serverRetryWarning && serverRetryWarning !== RETRY_RISK_WARNING
      ? serverRetryWarning
      : null;

  return (
    <>
      <div
        className={`session-workspace${traceOpen ? " has-trace" : ""}`}
      >
        <div className="session-conversation-column">
          <RunSummary
            canRetry={isLatestRun}
            commandPending={commandPending}
            onOpenTrace={() => setTraceOpen(true)}
            onRetry={() => {
              setRetryError(null);
              setRetryOpen(true);
            }}
            run={run}
            status={status}
            traceOpen={traceOpen}
          />

          {latestRuns.isError ? (
            <ErrorBanner
              message="最新运行状态加载失败。"
              onRetry={() => void latestRuns.refetch()}
            />
          ) : null}
          {runDetails.isError && currentRunId ? (
            <ErrorBanner
              message="Run 详情加载失败，消息仍可继续查看。"
              onRetry={() => void runDetails.refetch()}
            />
          ) : null}
          {streamNotice ? (
            <div className="run-stream-notice" role="status">
              <Icon name="refresh" size={15} />
              <span>{streamNotice}</span>
            </div>
          ) : null}
          {transientUnfinished ? (
            <div className="run-stream-notice" role="status">
              <Icon name="shield" size={15} />
              <span>
                {status === "failed"
                  ? "运行失败，未完成回答已保留在当前页面。"
                  : "运行已取消，未完成回答已保留在当前页面。"}
                刷新后不承诺恢复该文本。
              </span>
            </div>
          ) : null}
          {commandNotice ? (
            <div className="run-command-notice" role="status">
              <Icon name="inbox" size={15} />
              <span>{commandNotice}</span>
            </div>
          ) : null}
          {commandError ? <ErrorBanner message={commandError} /> : null}
          {terminalMessageFailure?.runId === currentRunId ? (
            <ErrorBanner
              message={
                terminalMessageFailure.kind === "not_found"
                  ? "运行已完成，但未找到完成事件所指的最终消息。清空草稿前请重试对账。"
                  : "运行已完成，但最终消息同步失败。请重试加载最终回答。"
              }
              onRetry={() =>
                setTerminalMessageRetry((attempt) => attempt + 1)
              }
              retryLabel="重试加载最终消息"
            />
          ) : null}

          <ChatMessageList
            error={messagesQuery.isError ? messagesQuery.error : null}
            hasMore={Boolean(messagesQuery.hasNextPage)}
            isLoading={messagesQuery.isPending}
            isLoadingMore={messagesQuery.isFetchingNextPage}
            messages={messages}
            onLoadMore={() => void messagesQuery.fetchNextPage()}
            onRetry={() => void messagesQuery.refetch()}
            sessionId={sessionId}
            transientText={transientText}
            transientUnfinished={transientUnfinished}
          />

          <SkillCatalogPicker
            disabled={commandPending || isActive || filePanelBusy}
            onChange={setSelectedSkillIds}
            selectedSkillIds={selectedSkillIds}
          />

          <SessionFilesPanel
            disabled={
              commandPending || (isActive && !isWaitingInput)
            }
            initialPendingUploads={handoffUploads}
            onBusyChange={setFilePanelBusy}
            onChangeSelectedFileIds={setSelectedFileIds}
            onInitialPendingUploadsChange={setHandoffUploads}
            selectedFileIds={new Set(selectedFileIds)}
            sessionId={sessionId}
          />

          {isWaitingInput && run ? (
            <section
              aria-labelledby="run-question-title"
              className="run-question"
            >
              <span className="empty-panel-kicker">Input requested</span>
              <h2 id="run-question-title">服务端请求补充信息</h2>
              {questionText ? (
                <MarkdownContent text={questionText} />
              ) : (
                <p>
                  当前问题消息尚未出现在已加载的历史窗口中；请结合服务端状态回答，页面不会伪造问题内容。
                </p>
              )}
            </section>
          ) : null}

          {isWaitingApproval && run ? (
            <ToolApprovalPanel
              approvals={run.pending_approvals}
              busyCallIds={busyApprovalIds}
              decisionPending={commandPending}
              errors={approvalErrors}
              onDecision={(approval, decision) =>
                void handleApproval(approval, decision)
              }
              runId={run.id}
              sessionId={sessionId}
            />
          ) : null}

          <section
            aria-busy={commandPending}
            aria-label={isWaitingInput ? "补充信息输入区" : "消息输入区"}
            className={`chat-composer${isActive ? " is-locked" : ""}`}
          >
            <header className="chat-composer-header">
              <label htmlFor="session-message">
                {isWaitingInput ? "补充信息" : "对话草稿"}
              </label>
              {isActive ? (
                <span className="chat-composer-lock">
                  <Icon name="lock" size={12} />
                  已锁定重复提交
                </span>
              ) : null}
            </header>
            <textarea
              id="session-message"
              onChange={(event) => setDraft(event.target.value)}
              placeholder={
                isWaitingInput
                  ? "回答服务端的当前提问。"
                  : "描述下一步目标，或继续当前对话。"
              }
              rows={4}
              value={draft}
            />
            <div className="chat-composer-footer">
              <ComposerHint
                hasPendingUploads={hasPendingUploads}
                status={status}
              />
              <div className="chat-composer-actions">
                {isActive && run ? (
                  <button
                    className="button-danger-outline"
                    disabled={commandPending}
                    onClick={() => void handleCancel()}
                    type="button"
                  >
                    {stopping
                      ? "正在请求停止…"
                      : status === "cancelling"
                        ? "再次请求停止"
                        : "停止任务"}
                  </button>
                ) : null}
                <button
                  className="button-primary"
                  disabled={!canSubmit}
                  onClick={() => void handleSubmit()}
                  type="button"
                >
                  {sending
                    ? isWaitingInput
                      ? "正在提交回答…"
                      : "正在提交…"
                    : isWaitingInput
                      ? "提交回答"
                      : isActive
                        ? "运行中，暂不可发送"
                        : "发送消息"}
                </button>
              </div>
            </div>
          </section>
        </div>

        <RunExecutionTrace
          onClose={() => setTraceOpen(false)}
          onOpen={() => setTraceOpen(true)}
          open={traceOpen}
          runId={currentRunId}
          sessionId={sessionId}
        />
      </div>

      <Modal
        closeDisabled={commandPending}
        description="此操作会从任务开头重新执行。"
        footer={
          <>
            <button
              className="button-quiet"
              disabled={commandPending}
              onClick={() => setRetryOpen(false)}
              type="button"
            >
              取消
            </button>
            <button
              className="button-danger"
              disabled={commandPending}
              onClick={() => void handleRetry()}
              type="button"
            >
              {retrying ? "正在创建重试…" : "确认从头重试"}
            </button>
          </>
        }
        onClose={() => {
          if (!commandPending) {
            setRetryOpen(false);
          }
        }}
        open={
          retryOpen &&
          Boolean(run && isLatestRun && status === "failed")
        }
        title="重试失败任务"
      >
        <div className="retry-risk">
          <p
            aria-label={
              serverRetryWarning === RETRY_RISK_WARNING
                ? "服务端重试警告"
                : undefined
            }
            role={
              serverRetryWarning === RETRY_RISK_WARNING ? "note" : undefined
            }
          >
            {RETRY_RISK_WARNING}
          </p>
          <ul>
            <li>工具写入、消息发送或其他副作用可能重复。</li>
            <li>原操作结果未知时，该操作也可能已经实际生效。</li>
            <li>系统不会跨工具回滚既有结果。</li>
          </ul>
          {distinctServerRetryWarning ? (
            <p
              aria-label="服务端重试警告"
              className="retry-server-warning"
              role="note"
            >
              {distinctServerRetryWarning}
            </p>
          ) : null}
          <p>
            确认后前端会发送 <code>confirm_unknown=true</code>，并切换到新的
            Run。
          </p>
        </div>
        {retryError ? <ErrorBanner message={retryError} /> : null}
      </Modal>
    </>
  );
}

export function SessionChatPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const session = useSession(sessionId);
  const projects = useProjectDirectory();

  if (!sessionId) {
    return null;
  }

  if (session.isError) {
    return (
      <div className="page-shell">
        <header className="page-heading">
          <div className="page-heading-copy">
            <p className="page-eyebrow">Workspace / Conversation</p>
            <h1>
              {isSessionNotFound(session.error) ? "会话不可用" : "会话加载失败"}
            </h1>
            <p className="page-description">
              当前页面不会保留失效的会话状态，也不会展示本地伪造的对话。
            </p>
          </div>
        </header>
        <ErrorBanner
          message={sessionErrorMessage(session.error)}
          onRetry={
            isSessionNotFound(session.error)
              ? undefined
              : () => void session.refetch()
          }
        />
        <Link className="button-secondary" to="/chat">
          返回新对话
        </Link>
      </div>
    );
  }

  const projectName = !session.data
    ? "正在加载项目…"
    : session.data.project_id
      ? projects.isPending
        ? "正在加载项目…"
        : (projects.data ?? []).find(
            (project) => project.id === session.data?.project_id,
          )?.name ?? "项目不可访问"
      : "未归属项目";

  return (
    <div
      aria-busy={session.isPending}
      className="page-shell session-page session-page--workspace"
    >
      <header className="page-heading session-page-heading">
        <div className="page-heading-copy">
          <p className="page-eyebrow">{projectName} / Conversation</p>
          <h1>
            {session.data
              ? sessionDisplayTitle(session.data.title)
              : "正在加载会话…"}
          </h1>
          <div className="session-page-meta">
            {session.data ? (
              <>
                <span>创建于 {formatDateTime(session.data.created_at)}</span>
                <span>更新于 {formatDateTime(session.data.updated_at)}</span>
                <span>标题保持后端原值，不会自动生成</span>
              </>
            ) : (
              <span role="status">正在并行读取消息与最新运行状态…</span>
            )}
          </div>
        </div>
        {session.data?.project_id ? (
          <Link
            className="button-quiet"
            to={`/projects/${encodeURIComponent(
              session.data.project_id,
            )}/overview`}
          >
            查看项目
          </Link>
        ) : null}
      </header>

      <SessionConversation key={sessionId} sessionId={sessionId} />
    </div>
  );
}

