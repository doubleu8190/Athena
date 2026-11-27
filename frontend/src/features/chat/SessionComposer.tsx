import {
  FileText,
  FolderOpen,
  Paperclip,
  RefreshCw,
  Upload,
} from "lucide-react";
import {
  forwardRef,
  memo,
  useCallback,
  useEffect,
  useId,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";

import { formatBytes } from "../../shared/lib/format";
import {
  Badge,
  Button,
  Checkbox,
  InlineAlert,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Tooltip,
  cn,
} from "../../shared/ui";
import { fileErrorMessage } from "../files/errorMessages";
import {
  fileDisplayName,
  type FilePreviewTarget,
  type PendingSessionUpload,
} from "../files/model";
import { SessionFileDownloadButton } from "../files/SessionFileDownloadButton";
import { usePublicSkills } from "../skills/hooks";
import { ChatComposer, type ChatComposerProps } from "./ChatComposer";
import type { ComposerSkillChip } from "./ComposerAttachmentBar";
import type { PendingSubagentInput, RunStatus } from "./model";
import { useSessionUploadQueue } from "./useSessionUploadQueue";

export { ChatComposer, type ChatComposerProps };

function resolveComposerHint({
  failedUploadCount,
  status,
  targetedSubagentName,
  uploadingCount,
}: {
  failedUploadCount: number;
  status: RunStatus | null;
  targetedSubagentName: string | null;
  uploadingCount: number;
}): string {
  if (uploadingCount > 0) {
    return "正在上传附件，完成后即可发送…";
  }
  if (failedUploadCount > 0) {
    return "有附件上传失败，请先重试或移除后再发送。";
  }
  if (targetedSubagentName) {
    return `正在回复「${targetedSubagentName}」的提问，提交后将自动继续。`;
  }
  if (status === "queued") {
    return "任务排队中，完成后可继续对话。";
  }
  if (status === "running") {
    return "正在处理中，可先编辑下一条消息。";
  }
  if (status === "waiting_for_subagents") {
    return "正在等待子智能体完成任务，完成后将自动继续回复；您可随时点击停止以取消任务";
  }
  if (status === "waiting_for_input") {
    return "请补充相关信息后提交，助手将继续处理。";
  }
  if (status === "waiting_for_approval") {
    return "请先确认上方的操作审批，全部处理后将自动继续。";
  }
  if (status === "cancelling") {
    return "正在停止当前任务，请稍候…";
  }
  return "Enter 发送 · Shift + Enter 换行";
}

export interface SessionComposerHandle {
  /** 从历史用户消息回填草稿文本并立即聚焦输入框（供「编辑重发」使用，不触发外层消息列表重渲染）。 */
  fillDraft: (text: string) => void;
}

export interface SessionComposerProps {
  activeSubagentInput: PendingSubagentInput | null;
  canSendWithoutDraft: boolean;
  commandPending: boolean;
  hasProjectContext?: boolean;
  hasRun: boolean;
  initialDraft: string;
  initialPendingUploads?: PendingSessionUpload[];
  isActive: boolean;
  isTargetedSubagentAnswer: boolean;
  isWaitingInput: boolean;
  isWaitingSubagents: boolean;
  knowledgeBaseIds: string[] | null;
  modelProfileId: string | null;
  onBlockingUploadsChange?: (hasBlockingUploads: boolean) => void;
  onCancel: () => Promise<void> | void;
  onChangeKnowledgeBaseIds: (knowledgeBaseIds: string[] | null) => void;
  onChangeModelProfileId: (modelProfileId: string | null) => void;
  onChangeSelectedFileIds: (fileIds: string[]) => void;
  onChangeSelectedSkillIds: (skillIds: string[]) => void;
  onPendingUploadsChange?: (uploads: PendingSessionUpload[]) => void;
  onPreviewFile?: (target: FilePreviewTarget) => void;
  onSubmit: (submittedDraft: string) => Promise<boolean>;
  selectedFileIds: string[];
  selectedSkillIds: string[];
  sending: boolean;
  sessionId: string;
  status: RunStatus | null;
  sticky?: boolean;
  stopping: boolean;
}

/**
 * 会话页吸底一体化输入器：
 * - 独立承载高频击键的 draft 状态，避免输入过程触发消息历史与进度条重渲染；
 * - 包裹 React.memo 隔离流式 text_delta 高频刷新对输入器子树的穿透；
 * - 通过 forwardRef 暴露 fillDraft 命令句柄，支持用户消息「编辑重发」无额外重渲染回填并聚焦；
 * - 吸收旧版 SessionFilesPanel：选文件/拖入/粘贴后立即调用 uploadSessionFile 自动上传，
 *   并在 📎 菜单中支持勾选引用本会话已有文件与产物。
 */
export const SessionComposer = memo(
  forwardRef<SessionComposerHandle, SessionComposerProps>(
    function SessionComposer(
      {
        activeSubagentInput,
        canSendWithoutDraft,
        commandPending,
        hasProjectContext = false,
        hasRun,
        initialDraft,
        initialPendingUploads = [],
        isActive,
        isTargetedSubagentAnswer,
        isWaitingInput,
        isWaitingSubagents,
        knowledgeBaseIds,
        modelProfileId,
        onBlockingUploadsChange,
        onCancel,
        onChangeKnowledgeBaseIds,
        onChangeModelProfileId,
        onChangeSelectedFileIds,
        onChangeSelectedSkillIds,
        onPendingUploadsChange,
        onPreviewFile,
        onSubmit,
        selectedFileIds,
        selectedSkillIds,
        sending,
        sessionId,
        status,
        sticky = true,
        stopping,
      },
      ref,
    ) {
      const [draft, setDraft] = useState(initialDraft);
      const [attachmentMenuOpen, setAttachmentMenuOpen] = useState(false);

      const attachmentMenuTitleId = useId();
      const textareaRef = useRef<HTMLTextAreaElement>(null);

      useImperativeHandle(
        ref,
        () => ({
          fillDraft(text: string) {
            setDraft(text);
            textareaRef.current?.focus();
            queueMicrotask(() => {
              const element = textareaRef.current;
              if (!element) {
                return;
              }
              element.focus();
              const length = element.value.length;
              element.setSelectionRange(length, length);
            });
          },
        }),
        [],
      );

      const attachmentsDisabled =
        commandPending ||
        (isActive && !isWaitingInput && !isTargetedSubagentAnswer);

      const {
        attachmentChips,
        dismissValidationErrors,
        failedUploadCount,
        fileInputRef,
        filesQuery,
        handleAddFiles,
        hasBlockingUploads,
        hasImageAttachment,
        imageAttachmentCount,
        selectedFileIdSet,
        sessionFiles,
        toggleExistingSessionFile,
        uploadingCount,
        validationErrors,
      } = useSessionUploadQueue({
        attachmentsDisabled,
        initialPendingUploads,
        onBlockingUploadsChange,
        onChangeSelectedFileIds,
        onPendingUploadsChange,
        onPreviewFile,
        selectedFileIds,
        sessionId,
      });

      const skillsQuery = usePublicSkills();

      const capabilityControlsDisabled =
        commandPending || isActive || uploadingCount > 0;
      const canSubmit =
        canSendWithoutDraft && !hasBlockingUploads && Boolean(draft.trim());

      useEffect(() => {
        if (attachmentsDisabled && attachmentMenuOpen) {
          setAttachmentMenuOpen(false);
        }
      }, [attachmentMenuOpen, attachmentsDisabled]);

      const skillChips = useMemo<ComposerSkillChip[]>(() => {
        const catalogMap = new Map(
          (skillsQuery.data ?? []).map((skill) => [skill.id, skill]),
        );
        return selectedSkillIds.map((skillId) => {
          const matched = catalogMap.get(skillId);
          const unavailable = skillsQuery.isSuccess && !matched;
          return {
            id: skillId,
            name: matched?.name ?? (unavailable ? "不可用技能" : skillId),
            onRemove: capabilityControlsDisabled
              ? undefined
              : () =>
                  onChangeSelectedSkillIds(
                    selectedSkillIds.filter((id) => id !== skillId),
                  ),
            unavailable,
          };
        });
      }, [
        capabilityControlsDisabled,
        onChangeSelectedSkillIds,
        selectedSkillIds,
        skillsQuery.data,
        skillsQuery.isSuccess,
      ]);

      const submitCurrentDraft = useCallback(async () => {
        const submittedDraft = draft;
        const accepted = await onSubmit(submittedDraft);
        if (accepted) {
          // 请求期间用户可能继续输入；仅清空仍与提交内容完全一致的草稿。
          setDraft((current) => (current === submittedDraft ? "" : current));
        }
      }, [draft, onSubmit]);

      const attachmentControl = (
        <>
          <input
            aria-label="选择会话文件"
            className="sr-only"
            disabled={attachmentsDisabled}
            multiple
            onChange={(event) => {
              const files = event.target.files
                ? Array.from(event.target.files)
                : [];
              handleAddFiles(files);
            }}
            ref={fileInputRef}
            type="file"
          />
          <Popover
            onOpenChange={setAttachmentMenuOpen}
            open={attachmentMenuOpen}
          >
            <Tooltip content="上传或引用会话文件">
              <PopoverTrigger asChild>
                <button
                  aria-label="添加附件"
                  className={cn(
                    "inline-flex h-8 w-8 items-center justify-center rounded-lg border border-border-default bg-canvas text-text-secondary transition-colors",
                    "hover:border-border-strong hover:bg-subtle hover:text-text-primary",
                    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                    "disabled:cursor-not-allowed disabled:opacity-50",
                  )}
                  data-compact-control="true"
                  disabled={attachmentsDisabled}
                  type="button"
                >
                  <Paperclip aria-hidden="true" className="h-4 w-4" />
                </button>
              </PopoverTrigger>
            </Tooltip>

            <PopoverContent
              align="start"
              aria-labelledby={attachmentMenuTitleId}
              className="w-80 p-2.5"
              side="top"
            >
              <Button
                className="w-full justify-start"
                disabled={attachmentsDisabled}
                onClick={() => {
                  setAttachmentMenuOpen(false);
                  fileInputRef.current?.click();
                }}
                size="sm"
                variant="outline"
              >
                <Upload
                  aria-hidden="true"
                  className="h-4 w-4 text-text-tertiary"
                />
                <span>上传本地文件</span>
              </Button>

              <div className="my-2.5 h-px bg-border-subtle" />

              <div className="mb-1.5 flex items-center gap-1.5 px-1">
                <FolderOpen
                  aria-hidden="true"
                  className="h-3.5 w-3.5 text-text-tertiary"
                />
                <h2
                  className="text-xs font-semibold text-text-secondary"
                  id={attachmentMenuTitleId}
                >
                  引用本会话已有文件
                </h2>
              </div>

              {filesQuery.isPending ? (
                <p
                  className="px-2 py-3 text-xs text-text-secondary"
                  role="status"
                >
                  正在读取会话文件…
                </p>
              ) : filesQuery.isError ? (
                <InlineAlert
                  action={
                    <Button
                      disabled={attachmentsDisabled}
                      onClick={() => void filesQuery.refetch()}
                      size="xs"
                      variant="outline"
                    >
                      <RefreshCw aria-hidden="true" className="h-3 w-3" />
                      重试
                    </Button>
                  }
                  className="px-2.5 py-2 text-xs"
                  description={fileErrorMessage(filesQuery.error)}
                  variant="danger"
                />
              ) : sessionFiles.length === 0 ? (
                <p className="rounded-lg bg-subtle px-3 py-3 text-center text-xs text-text-secondary">
                  暂无已发布文件
                </p>
              ) : (
                <div className="max-h-56 space-y-1 overflow-y-auto pr-0.5">
                  {sessionFiles.map((file) => {
                    const label = fileDisplayName(file);
                    const checked = selectedFileIdSet.has(file.id);
                    const checkboxId = `${attachmentMenuTitleId}-file-${file.id}`;
                    return (
                      <div
                        className={cn(
                          "flex items-center justify-between gap-2 rounded-lg border px-2 py-1.5 transition-colors",
                          checked
                            ? "border-brand-border bg-brand-subtle/60"
                            : "border-transparent hover:bg-subtle",
                        )}
                        key={file.id}
                      >
                        <label
                          className={cn(
                            "flex min-w-0 flex-1 items-start gap-2",
                            attachmentsDisabled
                              ? "cursor-not-allowed opacity-60"
                              : "cursor-pointer",
                          )}
                          htmlFor={checkboxId}
                        >
                          <Checkbox
                            aria-label={label}
                            checked={checked}
                            className="mt-0.5"
                            disabled={attachmentsDisabled}
                            id={checkboxId}
                            onCheckedChange={() =>
                              toggleExistingSessionFile(file.id)
                            }
                            onKeyDown={(event) => {
                              if (event.key === "Enter") {
                                event.preventDefault();
                              }
                            }}
                          />
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-1.5">
                              <FileText
                                aria-hidden="true"
                                className="h-3.5 w-3.5 shrink-0 text-text-tertiary"
                              />
                              <span className="truncate text-xs font-medium text-text-primary">
                                {label}
                              </span>
                              {file.extraction_status === "ready" ? (
                                <Badge
                                  className="px-1 py-0 text-[10px]"
                                  variant="success"
                                >
                                  已解析
                                </Badge>
                              ) : file.extraction_status === "failed" ? (
                                <Badge
                                  className="px-1 py-0 text-[10px]"
                                  variant="warning"
                                >
                                  未提取
                                </Badge>
                              ) : null}
                            </div>
                            <span className="block truncate text-[11px] text-text-tertiary">
                              {formatBytes(file.size_bytes)} ·{" "}
                              {file.source === "artifact"
                                ? "技能产物"
                                : "用户上传"}
                            </span>
                          </div>
                        </label>
                        {/* 下载按钮独立置于复选框标签之外，避免嵌套交互冲突并保留完整会话文件下载入口。 */}
                        <div className="shrink-0">
                          <SessionFileDownloadButton
                            compact
                            fileId={file.id}
                            filename={label}
                            sessionId={sessionId}
                          />
                        </div>
                      </div>
                    );
                  })}
                  {filesQuery.hasNextPage ? (
                    <Button
                      className="mt-1 w-full"
                      disabled={
                        attachmentsDisabled || filesQuery.isFetchingNextPage
                      }
                      onClick={() => void filesQuery.fetchNextPage()}
                      size="xs"
                      variant="ghost"
                    >
                      {filesQuery.isFetchingNextPage
                        ? "正在加载更多…"
                        : "加载更多会话文件"}
                    </Button>
                  ) : null}
                </div>
              )}
            </PopoverContent>
          </Popover>
        </>
      );

      const textareaLabel = activeSubagentInput
        ? `正在回答子智能体「${activeSubagentInput.subagent_name}」的提问`
        : isWaitingInput
          ? "补充信息"
          : "对话草稿";

      const contextBanner = activeSubagentInput ? (
        <span className="font-medium text-brand">
          正在回复子智能体「{activeSubagentInput.subagent_name}」的提问
        </span>
      ) : isWaitingInput ? (
        <span className="font-medium text-brand">
          请补充信息以继续当前任务
        </span>
      ) : null;

      const placeholder = activeSubagentInput
        ? `输入对「${activeSubagentInput.subagent_name}」的回答…`
        : isWaitingInput
          ? "请输入补充信息…"
          : isWaitingSubagents
            ? "正在等待子任务完成…"
            : "继续提问、粘贴截图或拖入资料…";

      const submitButtonLabel = sending
        ? isWaitingInput || isTargetedSubagentAnswer
          ? "正在提交回答…"
          : "正在发送…"
        : activeSubagentInput
          ? `提交给「${activeSubagentInput.subagent_name}」`
          : isWaitingInput
            ? "提交回答"
            : isWaitingSubagents
              ? "等待子智能体完成…"
              : isActive
                ? "运行中，暂不可发送"
                : "发送消息";

      const cancelButtonLabel = stopping
        ? "正在停止…"
        : status === "cancelling"
          ? "再次请求停止"
          : "停止任务";

      const footerHint = resolveComposerHint({
        failedUploadCount,
        status,
        targetedSubagentName: activeSubagentInput?.subagent_name ?? null,
        uploadingCount,
      });

      return (
        <ChatComposer
          ariaLabel={
            activeSubagentInput
              ? `正在回答子智能体「${activeSubagentInput.subagent_name}」的提问`
              : isWaitingInput
                ? "补充信息输入区"
                : "消息输入区"
          }
          attachmentControl={attachmentControl}
          attachments={attachmentChips}
          attachmentsDisabled={attachmentsDisabled}
          busy={commandPending || uploadingCount > 0}
          cancelButtonDisabled={commandPending}
          cancelButtonLabel={cancelButtonLabel}
          canSubmit={canSubmit}
          capabilityControlsDisabled={capabilityControlsDisabled}
          contextBanner={contextBanner}
          draft={draft}
          footerHint={footerHint}
          hasImageAttachment={hasImageAttachment}
          imageAttachmentCount={imageAttachmentCount}
          hasProjectContext={hasProjectContext}
          isLocked={isActive && !isTargetedSubagentAnswer && !isWaitingInput}
          knowledgeBaseIds={knowledgeBaseIds}
          modelProfileId={modelProfileId}
          onAddFiles={handleAddFiles}
          onCancel={onCancel}
          onChangeDraft={setDraft}
          onChangeKnowledgeBaseIds={onChangeKnowledgeBaseIds}
          onChangeModelProfileId={onChangeModelProfileId}
          onChangeSelectedSkillIds={onChangeSelectedSkillIds}
          onDismissValidationErrors={dismissValidationErrors}
          onSubmit={submitCurrentDraft}
          placeholder={placeholder}
          selectedSkillIds={selectedSkillIds}
          showCancelButton={isActive && hasRun}
          skillChips={skillChips}
          skillDisabledNote={
            isActive
              ? "任务运行中，本次指定技能已锁定；下一轮可继续调整。"
              : "当前操作进行中，下一轮可调整技能。"
          }
          sticky={sticky}
          submitButtonLabel={submitButtonLabel}
          submitLoading={sending}
          textareaId="session-message"
          textareaLabel={textareaLabel}
          textareaRef={textareaRef}
          validationErrors={validationErrors}
        />
      );
    },
  ),
);
