import { useQueryClient } from "@tanstack/react-query";
import { RefreshCw, RotateCcw, XCircle } from "lucide-react";
import { useCallback, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { Button, EmptyState, InlineAlert, toast } from "../../shared/ui";
import { FilePreviewDialog } from "../files/FilePreviewDialog";
import { syncSessionFileInCache, useSessionFiles } from "../files/hooks";
import type { FilePreviewTarget, SessionFile } from "../files/model";
import { sessionFileKeys } from "../files/queryKeys";
import {
  isSessionNotFound,
  sessionErrorMessage,
} from "../sessions/errorMessages";
import { useSession } from "../sessions/hooks";
import type { Session } from "../sessions/model";
import { ChatHeader } from "./ChatHeader";
import { ChatMessageList } from "./ChatMessageList";
import { ExecutionProgressAccordion } from "./ExecutionProgressAccordion";
import { FailedRunCard, RetryRunAlertDialog } from "./FailedRunCard";
import { messageText, type PendingSubagentInput } from "./model";
import {
  SessionComposer,
  type SessionComposerHandle,
} from "./SessionComposer";
import { SessionFilesDrawer } from "./SessionFilesDrawer";
import { SubagentApprovalPanel, subagentInputKey } from "./SubagentHitlPanels";
import { ToolApprovalPanel } from "./ToolApprovalPanel";
import { useSessionChatController } from "./useSessionChatController";
import { WaitingInputBanner } from "./WaitingInputBanner";

interface SessionWorkbenchProps {
  session: Session | null;
  sessionId: string;
}

function SessionWorkbench({ session, sessionId }: SessionWorkbenchProps) {
  const queryClient = useQueryClient();
  const [filesDrawerOpen, setFilesDrawerOpen] = useState(false);
  // 统一提升至会话工作台顶层挂载单个 FilePreviewDialog，由消息流、执行进度、输入器与右侧文件抽屉共用；
  // 预览打开期间文件抽屉以 open={filesDrawerOpen && !previewTarget} 主动让位卸载，彻底消除两个兄弟 Radix
  // 模态层同时挂载引发的 DismissableLayer / Escape 竞态与 FocusScope 争抢。
  const [previewTarget, setPreviewTarget] = useState<FilePreviewTarget | null>(
    null,
  );
  const previewTargetRef = useRef<FilePreviewTarget | null>(null);
  previewTargetRef.current = previewTarget;
  // 仅当预览由 SessionFilesDrawer 内部触发时记录对应文件 ID，关闭预览并恢复抽屉后精准归还焦点；
  // 非抽屉入口打开预览时会清空此标记，确保完全保留 FilePreviewDialog 自身的原触发元素回焦逻辑。
  const [drawerRestoreFocusFileId, setDrawerRestoreFocusFileId] = useState<
    string | null
  >(null);
  const composerRef = useRef<SessionComposerHandle>(null);

  const sessionFilesQuery = useSessionFiles(sessionId, Boolean(sessionId));
  const sessionFilesById = useMemo<ReadonlyMap<string, SessionFile>>(() => {
    const map = new Map<string, SessionFile>();
    for (const page of sessionFilesQuery.data?.pages ?? []) {
      for (const file of page.items) {
        map.set(file.id, file);
      }
    }
    return map;
  }, [sessionFilesQuery.data?.pages]);

  const handleOpenFilesDrawer = useCallback(() => {
    setDrawerRestoreFocusFileId(null);
    setFilesDrawerOpen(true);
  }, []);

  const handleFilesDrawerOpenChange = useCallback((nextOpen: boolean) => {
    // 顶层预览打开期间抽屉处于受控让位态，屏蔽期间的开关回调以免误清空用户的抽屉打开意图。
    if (previewTargetRef.current) {
      return;
    }
    if (!nextOpen) {
      setDrawerRestoreFocusFileId(null);
    }
    setFilesDrawerOpen(nextOpen);
  }, []);

  const handleDrawerFocusRestored = useCallback(() => {
    setDrawerRestoreFocusFileId(null);
  }, []);

  const handlePreviewFile = useCallback((target: FilePreviewTarget) => {
    previewTargetRef.current = target;
    setDrawerRestoreFocusFileId(null);
    setPreviewTarget(target);
  }, []);

  const handlePreviewFileFromDrawer = useCallback(
    (target: FilePreviewTarget) => {
      previewTargetRef.current = target;
      setDrawerRestoreFocusFileId(target.fileId);
      setPreviewTarget(target);
    },
    [],
  );

  const handlePreviewDialogOpenChange = useCallback((nextOpen: boolean) => {
    if (!nextOpen) {
      previewTargetRef.current = null;
      setPreviewTarget(null);
    }
  }, []);

  const handlePreviewMetadataUpdated = useCallback(
    (updatedFile: SessionFile) => {
      syncSessionFileInCache(queryClient, sessionId, updatedFile);
    },
    [queryClient, sessionId],
  );

  const handlePreviewExtractionFailed = useCallback(() => {
    void queryClient.invalidateQueries({
      queryKey: sessionFileKeys.list(sessionId),
    });
  }, [queryClient, sessionId]);

  const controller = useSessionChatController({
    sessionId,
    sessionTitle: session ? session.title : undefined,
  });

  const {
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
    initialDraft,
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
  } = controller;

  const triggerMessageText = useMemo(() => {
    if (!run?.trigger_message_id) {
      return null;
    }
    const triggerMessage = messages.find(
      (message) => message.id === run.trigger_message_id,
    );
    return triggerMessage ? messageText(triggerMessage).trim() || null : null;
  }, [messages, run?.trigger_message_id]);

  // 稳定回调保证 ChatMessageList、WaitingInputBanner 与历史 MessageRow 保持记忆化隔离。
  const handleEditResend = useCallback((text: string) => {
    composerRef.current?.fillDraft(text);
  }, []);

  const handleSelectSubagentInput = useCallback(
    (item: PendingSubagentInput) => {
      setSelectedSubagentInputKey(subagentInputKey(item));
    },
    [setSelectedSubagentInputKey],
  );

  // 仅当最新一轮失败，或已取消且当前消息分页包含触发提问文本时，助手消息才展示「重新执行」快捷操作。
  const canRetryFromAssistantMessage =
    isLatestRun &&
    (status === "failed" ||
      (status === "cancelled" && Boolean(triggerMessageText)));

  const handleRetryFromAssistantMessage = useCallback(() => {
    if (status === "failed" && isLatestRun) {
      openRetryDialog();
      return;
    }
    if (status === "cancelled" && triggerMessageText) {
      composerRef.current?.fillDraft(triggerMessageText);
      toast.info("原任务已取消，已为您回填提问以便重新发送");
    }
  }, [isLatestRun, openRetryDialog, status, triggerMessageText]);

  const progressSlot = useMemo(() => {
    // 零 Run 空会话必须返回 null，确保 ChatMessageList 正常渲染「开始这段对话」空态。
    if (!run || !status) {
      return null;
    }
    return (
      <ExecutionProgressAccordion
        onPreviewFile={handlePreviewFile}
        run={run}
        sessionId={sessionId}
        status={status}
        subagentTasks={subagentTasks}
      />
    );
  }, [handlePreviewFile, run, sessionId, status, subagentTasks]);

  const topAlertSlot = useMemo(() => {
    const hasTopAlerts =
      latestRunsIsError ||
      (runDetailsIsError && Boolean(currentRunId)) ||
      Boolean(streamNotice) ||
      transientUnfinished ||
      terminalMessageFailure?.runId === currentRunId;

    if (!hasTopAlerts) {
      return null;
    }

    return (
      <div className="space-y-2.5">
        {latestRunsIsError ? (
          <InlineAlert
            action={
              <Button
                onClick={refetchLatestRuns}
                size="xs"
                variant="outline"
              >
                <RefreshCw aria-hidden="true" className="h-3 w-3" />
                重试
              </Button>
            }
            description="最新任务状态加载失败，请重试。"
            variant="danger"
          />
        ) : null}

        {runDetailsIsError && currentRunId ? (
          <InlineAlert
            action={
              <Button
                onClick={refetchRunDetails}
                size="xs"
                variant="outline"
              >
                <RefreshCw aria-hidden="true" className="h-3 w-3" />
                重试
              </Button>
            }
            description="任务详情同步失败，历史消息仍可正常阅读。"
            variant="warning"
          />
        ) : null}

        {streamNotice ? (
          <InlineAlert description={streamNotice} variant="warning" />
        ) : null}

        {transientUnfinished ? (
          <InlineAlert
            description={
              status === "failed"
                ? "任务执行未完成，已生成的临时内容保留在当前页面，刷新后不承诺恢复。"
                : "任务已取消，已生成的临时内容保留在当前页面，刷新后不承诺恢复。"
            }
            variant="warning"
          />
        ) : null}

        {terminalMessageFailure?.runId === currentRunId ? (
          <InlineAlert
            action={
              <Button
                onClick={retryTerminalReconcile}
                size="xs"
                variant="outline"
              >
                {status === "failed" ? "重试加载失败报告" : "重试加载最终消息"}
              </Button>
            }
            description={
              status === "failed"
                ? terminalMessageFailure.kind === "not_found"
                  ? "任务失败，但未找到对应的权威说明。清空草稿前请重试同步。"
                  : "失败说明同步失败，请重试加载。"
                : terminalMessageFailure.kind === "not_found"
                  ? "任务已完成，但未找到对应的最终回复。清空草稿前请重试同步。"
                  : "任务已完成，但最终回复同步失败，请重试加载。"
            }
            variant="danger"
          />
        ) : null}
      </div>
    );
  }, [
    currentRunId,
    latestRunsIsError,
    refetchLatestRuns,
    refetchRunDetails,
    retryTerminalReconcile,
    runDetailsIsError,
    status,
    streamNotice,
    terminalMessageFailure,
    transientUnfinished,
  ]);

  const footerSlot = useMemo(() => {
    const showMainApprovals = isWaitingApproval && Boolean(run);
    const showSubagentApprovals = pendingSubagentApprovals.length > 0;
    const showFailedCard = status === "failed" && Boolean(run);
    const showCancelledCard = status === "cancelled" && Boolean(run);
    const showActiveRetryWarning =
      !showFailedCard && Boolean(run?.retry_warning?.trim());

    if (
      !showMainApprovals &&
      !showSubagentApprovals &&
      !showFailedCard &&
      !showCancelledCard &&
      !showActiveRetryWarning
    ) {
      return null;
    }

    return (
      <div className="space-y-4">
        {showActiveRetryWarning && run?.retry_warning ? (
          <InlineAlert
            aria-label="服务端重试警告"
            description={run.retry_warning.trim()}
            role="note"
            variant="warning"
          />
        ) : null}

        {showSubagentApprovals ? (
          <SubagentApprovalPanel
            approvals={pendingSubagentApprovals}
            busyKeys={busySubagentApprovalKeys}
            errors={subagentApprovalErrors}
            onDecision={handleSubagentApproval}
            sessionId={sessionId}
          />
        ) : null}

        {showMainApprovals && run ? (
          <ToolApprovalPanel
            approvals={run.pending_approvals}
            busyCallIds={busyApprovalIds}
            decisionPending={commandPending}
            errors={approvalErrors}
            onDecision={handleApproval}
            runId={run.id}
            sessionId={sessionId}
          />
        ) : null}

        {showFailedCard && run ? (
          <FailedRunCard
            canRetry={isLatestRun}
            commandPending={commandPending}
            onOpenRetry={openRetryDialog}
            run={run}
          />
        ) : null}

        {showCancelledCard ? (
          <section
            aria-label="任务已取消"
            className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border-default bg-subtle/70 px-4 py-3 text-xs text-text-secondary"
          >
            <span className="inline-flex items-center gap-2 font-medium text-text-primary">
              <XCircle
                aria-hidden="true"
                className="h-4 w-4 text-text-tertiary"
              />
              <span>本次任务已取消</span>
            </span>
            {isLatestRun && triggerMessageText ? (
              <Button
                onClick={() => {
                  composerRef.current?.fillDraft(triggerMessageText);
                }}
                size="xs"
                variant="outline"
              >
                <RotateCcw aria-hidden="true" className="h-3 w-3" />
                重新编辑发送
              </Button>
            ) : null}
          </section>
        ) : null}
      </div>
    );
  }, [
    approvalErrors,
    busyApprovalIds,
    busySubagentApprovalKeys,
    commandPending,
    handleApproval,
    handleSubagentApproval,
    isLatestRun,
    isWaitingApproval,
    openRetryDialog,
    pendingSubagentApprovals,
    run,
    sessionId,
    status,
    subagentApprovalErrors,
    triggerMessageText,
  ]);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-canvas">
      {/* 仅在 5 个生命周期节点更新的屏幕阅读器礼貌播报区，杜绝按流式 Token 刷屏。 */}
      <div aria-atomic="true" aria-live="polite" className="sr-only">
        {liveAnnouncement}
      </div>

      <ChatHeader
        hasActiveRun={isActive}
        onOpenFilesDrawer={handleOpenFilesDrawer}
        session={session}
        sessionId={sessionId}
      />

      {/* 中间唯一纵向滚动区：消除嵌套滚动条，消息居中 max-w-[780px]。 */}
      <ChatMessageList
        autoScrollRequest={autoScrollRequest}
        beforeLatestAssistantSlot={progressSlot}
        currentRunId={currentRunId}
        currentRunModelLabel={currentRunModelLabel}
        currentRunStatus={status}
        error={messagesQuery.isError ? messagesQuery.error : null}
        footerSlot={footerSlot}
        hasMore={Boolean(messagesQuery.hasNextPage)}
        isLatestRun={isLatestRun}
        isLoading={messagesQuery.isPending}
        isLoadingMore={messagesQuery.isFetchingNextPage}
        key={sessionId}
        messages={messages}
        onEditResend={handleEditResend}
        onLoadMore={handleLoadMoreMessages}
        onPreviewFile={handlePreviewFile}
        onRetry={handleRetryMessages}
        onRetryRun={
          canRetryFromAssistantMessage
            ? handleRetryFromAssistantMessage
            : undefined
        }
        sessionFilesById={sessionFilesById}
        sessionId={sessionId}
        topAlertSlot={topAlertSlot}
        transientText={transientText}
        transientUnfinished={transientUnfinished}
      />

      {/* 底部固定吸底区：澄清问答引导条 + 命令反馈 + 一体化输入器。 */}
      <div className="shrink-0 bg-canvas">
        <div className="mx-auto w-full max-w-[780px] px-4 pt-1 pb-3 sm:px-6">
          <WaitingInputBanner
            activeSubagentInput={activeSubagentInput}
            isWaitingMainInput={isWaitingInput && Boolean(run)}
            mainQuestionText={questionText}
            onSelectSubagentInput={handleSelectSubagentInput}
            pendingSubagentInputs={pendingSubagentInputs}
          />

          {commandNotice ? (
            <div className="mb-2">
              <InlineAlert description={commandNotice} variant="info" />
            </div>
          ) : null}

          {commandError ? (
            <div className="mb-2">
              <InlineAlert description={commandError} variant="danger" />
            </div>
          ) : null}

          <SessionComposer
            activeSubagentInput={activeSubagentInput}
            canSendWithoutDraft={canSendWithoutDraft}
            commandPending={commandPending}
            hasProjectContext={Boolean(session?.project_id)}
            hasRun={Boolean(run)}
            initialDraft={initialDraft}
            initialPendingUploads={handoffUploads}
            isActive={isActive}
            isTargetedSubagentAnswer={isTargetedSubagentAnswer}
            isWaitingInput={isWaitingInput}
            isWaitingSubagents={isWaitingSubagents}
            knowledgeBaseIds={knowledgeBaseIds}
            modelProfileId={modelProfileId}
            onBlockingUploadsChange={setHasBlockingUploads}
            onCancel={handleCancel}
            onChangeKnowledgeBaseIds={setKnowledgeBaseIds}
            onChangeModelProfileId={setModelProfileId}
            onChangeSelectedFileIds={setSelectedFileIds}
            onChangeSelectedSkillIds={setSelectedSkillIds}
            onPendingUploadsChange={setHandoffUploads}
            onPreviewFile={handlePreviewFile}
            onSubmit={handleSubmit}
            ref={composerRef}
            selectedFileIds={selectedFileIds}
            selectedSkillIds={selectedSkillIds}
            sending={sending}
            sessionId={sessionId}
            status={status}
            sticky={false}
            stopping={stopping}
          />
        </div>
      </div>

      <SessionFilesDrawer
        onFocusRestored={handleDrawerFocusRestored}
        onOpenChange={handleFilesDrawerOpenChange}
        onPreviewFile={handlePreviewFileFromDrawer}
        open={filesDrawerOpen && !previewTarget}
        restoreFocusFileId={drawerRestoreFocusFileId}
        sessionId={sessionId}
      />

      <FilePreviewDialog
        file={previewTarget}
        onExtractionFailed={handlePreviewExtractionFailed}
        onFileMetadataUpdated={handlePreviewMetadataUpdated}
        onOpenChange={handlePreviewDialogOpenChange}
        open={Boolean(previewTarget)}
        sessionId={sessionId}
      />

      <RetryRunAlertDialog
        commandPending={commandPending}
        onClose={closeRetryDialog}
        onConfirmRetry={handleRetry}
        open={retryOpen && Boolean(run && isLatestRun && status === "failed")}
        retryError={retryError}
        retrying={retrying}
        serverRetryWarning={run?.retry_warning ?? null}
      />
    </div>
  );
}

export function SessionChatPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const session = useSession(sessionId);

  if (!sessionId) {
    return null;
  }

  if (session.isError) {
    const notFound = isSessionNotFound(session.error);
    return (
      <div className="flex h-full min-h-0 flex-col items-center justify-center bg-canvas px-4 py-12">
        <div className="w-full max-w-md">
          <EmptyState
            action={
              notFound ? (
                <Button asChild variant="primary">
                  <Link to="/chat">开启新对话</Link>
                </Button>
              ) : (
                <>
                  <Button
                    onClick={() => void session.refetch()}
                    variant="primary"
                  >
                    重新加载
                  </Button>
                  <Button asChild variant="outline">
                    <Link to="/chat">开启新对话</Link>
                  </Button>
                </>
              )
            }
            description={
              notFound
                ? "该对话可能已被删除，或当前账号无权查看此内容。"
                : sessionErrorMessage(session.error)
            }
            icon="chat"
            title={notFound ? "对话不存在或当前无权访问" : "对话加载失败"}
          />
        </div>
      </div>
    );
  }

  return (
    <SessionWorkbench
      key={sessionId}
      session={session.data ?? null}
      sessionId={sessionId}
    />
  );
}
