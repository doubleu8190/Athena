import {
  AlertTriangle,
  ArrowUp,
  Eye,
  FileText,
  FolderOpen,
  Loader2,
  Lock,
  Paperclip,
  RefreshCw,
  Square,
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
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";

import { formatBytes } from "../../shared/lib/format";
import { newRequestId } from "../../shared/lib/requestId";
import {
  AutoResizeTextarea,
  Badge,
  Button,
  Checkbox,
  InlineAlert,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Tooltip,
  cn,
  toast,
} from "../../shared/ui";
import {
  fileErrorMessage,
  fileRequestCanBeReused,
} from "../files/errorMessages";
import { useSessionFiles, useUploadSessionFileMutation } from "../files/hooks";
import {
  MAX_VISION_IMAGES_PER_RUN,
  canInlinePreviewFile,
  fileDisplayName,
  isPreviewableImageFile,
  sessionFilePreviewUrl,
  sessionFileToPreviewTarget,
  validateSessionUpload,
  type FilePreviewTarget,
  type PendingSessionUpload,
  type SessionFile,
} from "../files/model";
import { SessionFileDownloadButton } from "../files/SessionFileDownloadButton";
import {
  resolveEffectiveUserModel,
  useUserModels,
  userModelDisplayLabel,
} from "../models";
import { usePublicSkills } from "../skills/hooks";
import { SkillCatalogPicker } from "../skills/SkillCatalogPicker";
import {
  ComposerAttachmentBar,
  isImageAttachmentFile,
  type ComposerAttachmentChip,
  type ComposerSkillChip,
} from "./ComposerAttachmentBar";
import { KnowledgeScopePopover } from "./KnowledgeScopePopover";
import type { PendingSubagentInput, RunStatus } from "./model";
import { ModelSelectorPopover } from "./ModelSelectorPopover";

interface SessionUploadTask {
  localId: string;
  file: File;
  requestId: string;
  reuseRequestId: boolean;
  status: "uploading" | "failed";
  errorMessage: string | null;
}

function hasDraggedFiles(dataTransfer: DataTransfer | null): boolean {
  if (!dataTransfer) {
    return false;
  }
  if (dataTransfer.files && dataTransfer.files.length > 0) {
    return true;
  }
  return Array.from(dataTransfer.types ?? []).includes("Files");
}

function normalizePastedFile(file: File, index: number): File {
  if (file.name.trim()) {
    return file;
  }
  // 部分浏览器粘贴剪贴板截图时文件名为空，按常见图片 MIME 补齐可读文件名以通过前端校验。
  const mime = file.type.toLowerCase();
  const ext =
    mime === "image/jpeg"
      ? "jpg"
      : mime === "image/webp"
        ? "webp"
        : mime === "image/gif"
          ? "gif"
          : mime === "image/png"
            ? "png"
            : "";
  if (!ext) {
    return file;
  }
  return new File([file], `pasted-image-${index + 1}.${ext}`, {
    lastModified: file.lastModified,
    type: file.type,
  });
}

function extractClipboardFiles(
  clipboardData: DataTransfer | null,
): File[] {
  if (!clipboardData) {
    return [];
  }
  const directFiles = Array.from(clipboardData.files ?? []);
  if (directFiles.length > 0) {
    return directFiles.map(normalizePastedFile);
  }
  const itemFiles: File[] = [];
  if (clipboardData.items) {
    for (const item of Array.from(clipboardData.items)) {
      if (item.kind === "file") {
        const file = item.getAsFile();
        if (file) {
          itemFiles.push(file);
        }
      }
    }
  }
  return itemFiles.map(normalizePastedFile);
}

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

export interface ChatComposerProps {
  /** 输入区无障碍区域标签。 */
  ariaLabel?: string;
  /** 左侧附件按钮或附件菜单控件（由 Draft/Session 模式分别注入）。 */
  attachmentControl: ReactNode;
  /** 顶部展示的附件芯片列表。 */
  attachments: readonly ComposerAttachmentChip[];
  /** 是否禁止拖入或粘贴新附件。 */
  attachmentsDisabled?: boolean;
  /** 是否处于繁忙提交态（设置 aria-busy）。 */
  busy?: boolean;
  /** 是否允许提交（需同时满足文本非空等条件）。 */
  canSubmit: boolean;
  /** 模型/知识库/技能三大能力选择器是否禁用。 */
  capabilityControlsDisabled?: boolean;
  /** 顶部上下文提示横幅（如补充输入或子智能体定向回答提示）。 */
  contextBanner?: ReactNode;
  /** 输入框是否禁用。 */
  disabled?: boolean;
  /** 当前草稿文本（受控）。 */
  draft: string;
  /** 外部提交失败错误提示。 */
  errorBanner?: string | null;
  /** 底部温和办公提示文案。 */
  footerHint: string;
  /** 当前是否包含图片附件（驱动非视觉模型琥珀提醒）。 */
  hasImageAttachment: boolean;
  /** 当前附件中的图片张数（驱动非识图提醒与超 5 张上限提示）。 */
  imageAttachmentCount?: number;
  /** 是否归属某个项目（影响知识库默认态文案）。 */
  hasProjectContext?: boolean;
  /** 是否锁定重复提交（用于顶部温和锁标识）。 */
  isLocked?: boolean;
  /** 知识库三态值。 */
  knowledgeBaseIds: string[] | null;
  /** 模型配置 ID。 */
  modelProfileId: string | null;
  /** 拖拽或粘贴文件时的回调。 */
  onAddFiles: (files: File[]) => void;
  /** 停止按钮点击回调（仅展示停止按钮时生效）。 */
  onCancel?: () => Promise<void> | void;
  onChangeDraft: (nextDraft: string) => void;
  onChangeKnowledgeBaseIds: (knowledgeBaseIds: string[] | null) => void;
  onChangeModelProfileId: (modelProfileId: string | null) => void;
  onChangeSelectedSkillIds: (skillIds: string[]) => void;
  /** 触发提交当前草稿。 */
  onSubmit: () => Promise<void> | void;
  placeholder: string;
  selectedSkillIds: string[];
  /** 是否显示停止按钮。 */
  showCancelButton?: boolean;
  cancelButtonDisabled?: boolean;
  cancelButtonLabel?: string;
  skillDisabledNote?: string;
  /** 展示在输入区顶部的已挂载技能芯片列表。 */
  skillChips: readonly ComposerSkillChip[];
  /** 是否吸底粘贴（会话页开启）。 */
  sticky?: boolean;
  submitButtonLabel: string;
  submitLoading?: boolean;
  textareaId?: string;
  textareaLabel: string;
  textareaRef?: RefObject<HTMLTextAreaElement>;
  /** 附件校验错误列表（非法格式、空文件、超限等）。 */
  validationErrors?: readonly string[];
  onDismissValidationErrors?: () => void;
}

/**
 * 一体化现代输入器视图原语（DraftChatPage 与 SessionComposer 共用）：
 * - 圆角卡片承载顶部附件/技能芯片、自增高文本框、左侧能力工具栏与右侧发送/停止按钮；
 * - 内置框内 Drag & Drop（dragDepthRef 防子元素抖动）与剪贴板图片/文件粘贴；
 * - 统一快捷键：Enter 直接发送 + Shift+Enter 换行，严格过滤 IME 组合态（isComposing / keyCode 229）。
 */
export function ChatComposer({
  ariaLabel = "消息输入区",
  attachmentControl,
  attachments,
  attachmentsDisabled = false,
  busy = false,
  cancelButtonDisabled = false,
  cancelButtonLabel = "停止任务",
  canSubmit,
  capabilityControlsDisabled = false,
  contextBanner,
  disabled = false,
  draft,
  errorBanner = null,
  footerHint,
  hasImageAttachment,
  imageAttachmentCount,
  hasProjectContext = false,
  isLocked = false,
  knowledgeBaseIds,
  modelProfileId,
  onAddFiles,
  onCancel,
  onChangeDraft,
  onChangeKnowledgeBaseIds,
  onChangeModelProfileId,
  onChangeSelectedSkillIds,
  onDismissValidationErrors,
  onSubmit,
  placeholder,
  selectedSkillIds,
  showCancelButton = false,
  skillChips,
  skillDisabledNote,
  sticky = false,
  submitButtonLabel,
  submitLoading = false,
  textareaId,
  textareaLabel,
  textareaRef,
  validationErrors = [],
}: ChatComposerProps) {
  const generatedTextareaId = useId();
  const resolvedTextareaId = textareaId ?? generatedTextareaId;
  const dragDepthRef = useRef(0);
  const [isDraggingOver, setIsDraggingOver] = useState(false);

  // 订阅用户模型目录（与 ModelSelectorPopover 共享同一 TanStack Query 缓存），驱动方案 A2 温和提示与一键切识图模型。
  const modelsQuery = useUserModels();
  const resolvedImageCount = imageAttachmentCount ?? (hasImageAttachment ? 1 : 0);
  const effectiveModel = useMemo(() => {
    const catalog = modelsQuery.data;
    if (!catalog) {
      return null;
    }
    if (
      modelsQuery.isSuccess &&
      modelProfileId !== null &&
      !catalog.items.some((item) => item.id === modelProfileId)
    ) {
      return null;
    }
    return resolveEffectiveUserModel(catalog, modelProfileId);
  }, [modelProfileId, modelsQuery.data, modelsQuery.isSuccess]);
  const recommendedVisionModel = useMemo(
    () =>
      (modelsQuery.data?.items ?? []).find((item) => item.supports_vision) ??
      null,
    [modelsQuery.data?.items],
  );
  const recommendedVisionLabel = recommendedVisionModel
    ? userModelDisplayLabel(recommendedVisionModel)
    : null;
  // 方案 A2 核心约定：非识图模型与超 5 张图片均仅展示温和非阻断提醒，绝不禁用发送按钮。
  const showVisionUnsupportedBanner =
    resolvedImageCount > 0 &&
    Boolean(effectiveModel && !effectiveModel.supports_vision);
  const showVisionLimitBanner = resolvedImageCount > MAX_VISION_IMAGES_PER_RUN;

  useEffect(() => {
    // 输入区切入禁用/锁定态时立即复位拖拽计数与遮罩，防止卡死在拖拽态。
    if (attachmentsDisabled) {
      dragDepthRef.current = 0;
      setIsDraggingOver(false);
    }
  }, [attachmentsDisabled]);

  useEffect(() => {
    if (!isDraggingOver) {
      return;
    }
    // 拖拽异常取消（移出窗口、失焦、窗口外松手或按 Escape）时 dragleave 可能缺失，
    // 通过全局兜底监听确保拖拽遮罩及时清除。
    const resetDragOverlay = () => {
      dragDepthRef.current = 0;
      setIsDraggingOver(false);
    };
    const handleWindowKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        resetDragOverlay();
      }
    };
    window.addEventListener("dragend", resetDragOverlay);
    window.addEventListener("drop", resetDragOverlay);
    window.addEventListener("blur", resetDragOverlay);
    window.addEventListener("keydown", handleWindowKeyDown);
    return () => {
      window.removeEventListener("dragend", resetDragOverlay);
      window.removeEventListener("drop", resetDragOverlay);
      window.removeEventListener("blur", resetDragOverlay);
      window.removeEventListener("keydown", handleWindowKeyDown);
    };
  }, [isDraggingOver]);

  const handleDragEnter = (event: DragEvent<HTMLDivElement>) => {
    if (attachmentsDisabled || !hasDraggedFiles(event.dataTransfer)) {
      return;
    }
    event.preventDefault();
    dragDepthRef.current += 1;
    setIsDraggingOver(true);
  };

  const handleDragOver = (event: DragEvent<HTMLDivElement>) => {
    if (attachmentsDisabled || !hasDraggedFiles(event.dataTransfer)) {
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  };

  const handleDragLeave = (event: DragEvent<HTMLDivElement>) => {
    if (dragDepthRef.current === 0) {
      return;
    }
    event.preventDefault();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) {
      setIsDraggingOver(false);
    }
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    const droppedFiles = Array.from(event.dataTransfer?.files ?? []);
    const isFileDrop =
      droppedFiles.length > 0 || hasDraggedFiles(event.dataTransfer);
    dragDepthRef.current = 0;
    setIsDraggingOver(false);
    if (!isFileDrop) {
      // 非文件拖拽（如拖入选中文本）直接放行，避免吞掉浏览器原生文本拖放行为。
      return;
    }
    event.preventDefault();
    if (attachmentsDisabled) {
      return;
    }
    if (droppedFiles.length > 0) {
      onAddFiles(droppedFiles);
    }
  };

  const handlePaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    if (attachmentsDisabled) {
      return;
    }
    const pastedFiles = extractClipboardFiles(event.clipboardData);
    if (pastedFiles.length === 0) {
      // 剪贴板仅含纯文本时保持浏览器默认粘贴，不作拦截。
      return;
    }
    const plainText = event.clipboardData.getData("text/plain");
    if (!plainText) {
      event.preventDefault();
    }
    onAddFiles(pastedFiles);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter") {
      return;
    }
    // 严格拦截中文/日文输入法组合态（含 Safari/WebKit 的 keyCode === 229），杜绝选字误发送。
    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) {
      return;
    }
    // Shift + Enter 保留默认换行，Alt + Enter 交给系统/浏览器。
    if (event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    if (canSubmit) {
      void onSubmit();
    }
  };

  return (
    <section
      aria-busy={busy}
      aria-label={ariaLabel}
      className={cn(
        "w-full",
        sticky &&
          "sticky bottom-0 z-[var(--z-sticky)] bg-canvas/95 pt-2 pb-1 backdrop-blur-xs",
      )}
    >
      <div
        className={cn(
          "relative rounded-2xl border bg-canvas shadow-xs transition-[border-color,box-shadow]",
          isDraggingOver
            ? "border-brand-hover ring-2 ring-brand-hover/15"
            : "border-border-default focus-within:border-brand-hover focus-within:shadow-sm",
          isLocked && "bg-subtle/40",
        )}
        data-dragging={isDraggingOver ? "true" : undefined}
        onDragEnter={handleDragEnter}
        onDragLeave={handleDragLeave}
        onDragOver={handleDragOver}
        onDrop={handleDrop}
      >
        {/* 拖入文件时的品牌绿虚线遮罩（pointer-events-none 防止遮罩触发子元素 dragleave 抖动）。 */}
        {isDraggingOver ? (
          <div
            className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center gap-2 rounded-2xl border-2 border-dashed border-brand-hover bg-brand-subtle/90 text-sm font-medium text-brand backdrop-blur-[1px]"
            data-testid="composer-drop-overlay"
          >
            <Upload aria-hidden="true" className="h-5 w-5" />
            <span>松开即可附加文件</span>
          </div>
        ) : null}

        {contextBanner ? (
          <div className="flex items-center justify-between gap-2 border-b border-border-subtle/80 px-3.5 py-2 text-xs text-text-secondary">
            <div className="min-w-0 flex-1">{contextBanner}</div>
            {isLocked ? (
              <span className="inline-flex shrink-0 items-center gap-1 rounded-md bg-subtle px-2 py-0.5 text-[11px] font-medium text-text-tertiary">
                <Lock aria-hidden="true" className="h-3 w-3" />
                处理中
              </span>
            ) : null}
          </div>
        ) : isLocked ? (
          <div className="flex items-center justify-end border-b border-border-subtle/70 px-3.5 py-1.5">
            <span className="inline-flex items-center gap-1 rounded-md bg-subtle px-2 py-0.5 text-[11px] font-medium text-text-tertiary">
              <Lock aria-hidden="true" className="h-3 w-3" />
              运行中已锁定发送
            </span>
          </div>
        ) : null}

        <ComposerAttachmentBar
          attachments={attachments}
          disabled={disabled}
          skills={skillChips}
        />

        {showVisionUnsupportedBanner || showVisionLimitBanner ? (
          <div className="space-y-1.5 border-b border-warning-border bg-warning-bg px-3.5 py-2 text-xs text-warning">
            {showVisionUnsupportedBanner ? (
              <div
                className="flex flex-wrap items-center justify-between gap-2"
                data-testid="composer-vision-unsupported-banner"
                role="status"
              >
                <span className="inline-flex min-w-0 items-start gap-1.5 leading-relaxed">
                  <AlertTriangle
                    aria-hidden="true"
                    className="mt-0.5 h-3.5 w-3.5 shrink-0"
                  />
                  <span>
                    {recommendedVisionModel
                      ? "当前模型暂不支持识图，仍可直接发送，或切换至识图模型"
                      : "当前模型暂不支持识图，仍可直接发送；当前暂无可用识图模型"}
                  </span>
                </span>
                {recommendedVisionModel && recommendedVisionLabel ? (
                  <button
                    className="inline-flex shrink-0 items-center gap-1 rounded-md border border-warning-border bg-canvas px-2 py-1 text-xs font-medium text-warning transition-colors hover:bg-subtle focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus disabled:cursor-not-allowed disabled:opacity-50"
                    data-compact-control="true"
                    disabled={capabilityControlsDisabled}
                    onClick={() => {
                      onChangeModelProfileId(recommendedVisionModel.id);
                      toast.success(
                        `已切换至识图模型「${recommendedVisionLabel}」`,
                      );
                    }}
                    type="button"
                  >
                    <Eye aria-hidden="true" className="h-3 w-3 shrink-0" />
                    <span>一键切至「{recommendedVisionLabel}」</span>
                  </button>
                ) : null}
              </div>
            ) : null}

            {showVisionLimitBanner ? (
              <div
                className="flex items-start gap-1.5 leading-relaxed"
                data-testid="composer-vision-limit-banner"
                role="status"
              >
                <AlertTriangle
                  aria-hidden="true"
                  className="mt-0.5 h-3.5 w-3.5 shrink-0"
                />
                <span>
                  单次对话最多同时识别 {MAX_VISION_IMAGES_PER_RUN}{" "}
                  张图片，超出部分本次不会发送给识图模型（仍会保留为会话文件）
                </span>
              </div>
            ) : null}
          </div>
        ) : null}

        <label className="sr-only" htmlFor={resolvedTextareaId}>
          {textareaLabel}
        </label>
        <AutoResizeTextarea
          className="border-0 bg-transparent px-4 pt-3 pb-2 text-sm leading-relaxed shadow-none hover:border-transparent focus-visible:border-transparent focus-visible:ring-0"
          disabled={disabled}
          id={resolvedTextareaId}
          maxHeight={200}
          minRows={2}
          onChange={(event) => onChangeDraft(event.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={placeholder}
          ref={textareaRef}
          value={draft}
        />

        <div className="flex flex-wrap items-center justify-between gap-2 px-3 pt-1 pb-3">
          <div className="flex flex-wrap items-center gap-1.5">
            {attachmentControl}
            <ModelSelectorPopover
              disabled={capabilityControlsDisabled}
              hasImageAttachment={hasImageAttachment}
              onChange={onChangeModelProfileId}
              value={modelProfileId}
            />
            <KnowledgeScopePopover
              disabled={capabilityControlsDisabled}
              hasProjectContext={hasProjectContext}
              onChange={onChangeKnowledgeBaseIds}
              value={knowledgeBaseIds}
            />
            <SkillCatalogPicker
              disabled={capabilityControlsDisabled}
              disabledNote={skillDisabledNote}
              onChange={onChangeSelectedSkillIds}
              selectedSkillIds={selectedSkillIds}
            />
          </div>

          <div className="ml-auto flex items-center gap-2">
            {showCancelButton && onCancel ? (
              <Button
                disabled={cancelButtonDisabled}
                onClick={() => void onCancel()}
                size="sm"
                variant="danger-outline"
              >
                <Square aria-hidden="true" className="h-3.5 w-3.5" />
                {cancelButtonLabel}
              </Button>
            ) : null}
            <Button
              aria-busy={submitLoading || undefined}
              disabled={!canSubmit || submitLoading}
              onClick={() => void onSubmit()}
              size="sm"
              variant="primary"
            >
              {submitLoading ? (
                <Loader2
                  aria-hidden="true"
                  className="h-4 w-4 animate-spin motion-reduce:animate-none"
                />
              ) : (
                <ArrowUp aria-hidden="true" className="h-4 w-4" />
              )}
              {submitButtonLabel}
            </Button>
          </div>
        </div>
      </div>

      {validationErrors.length > 0 ? (
        <div className="mt-2">
          <InlineAlert
            action={
              onDismissValidationErrors ? (
                <Button
                  onClick={onDismissValidationErrors}
                  size="xs"
                  variant="ghost"
                >
                  知道了
                </Button>
              ) : undefined
            }
            description={
              validationErrors.length === 1 ? (
                validationErrors[0]
              ) : (
                <ul className="list-disc pl-4">
                  {validationErrors.map((error, index) => (
                    <li key={`${index}-${error}`}>{error}</li>
                  ))}
                </ul>
              )
            }
            variant="danger"
          />
        </div>
      ) : null}

      {errorBanner ? (
        <div className="mt-2">
          <InlineAlert description={errorBanner} variant="danger" />
        </div>
      ) : null}

      <p className="mt-1.5 px-1 text-xs text-text-tertiary">{footerHint}</p>
    </section>
  );
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
  const [validationErrors, setValidationErrors] = useState<string[]>([]);
  const [knownUploadedFiles, setKnownUploadedFiles] = useState<
    Record<string, SessionFile>
  >({});
  // 承接草稿页首次上传失败移交下来的待上传文件，逐项保留各自 reuseRequestId（5xx/网络/未启动为 true，4xx 为 false）。
  const [uploadTasks, setUploadTasks] = useState<SessionUploadTask[]>(() =>
    initialPendingUploads.map((pending) => ({
      errorMessage: "上传未完成，请重试或移除",
      file: pending.file,
      localId: pending.requestId,
      requestId: pending.requestId,
      reuseRequestId: pending.reuseRequestId,
      status: "failed",
    })),
  );

  const attachmentMenuTitleId = useId();
  const fileInputRef = useRef<HTMLInputElement>(null);
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
  const mountedRef = useRef(true);
  const selectedFileIdsRef = useRef(selectedFileIds);
  selectedFileIdsRef.current = selectedFileIds;
  const uploadTasksRef = useRef(uploadTasks);
  uploadTasksRef.current = uploadTasks;
  // 同步防重入锁：记录当前正在上传中的任务 localId，彻底杜绝快速双击“重试”导致同文件并发双传。
  const inFlightUploadIdsRef = useRef<Set<string>>(new Set());

  const filesQuery = useSessionFiles(sessionId, Boolean(sessionId));
  const uploadMutation = useUploadSessionFileMutation(sessionId);
  const skillsQuery = usePublicSkills();

  useEffect(() => {
    mountedRef.current = true;
    const inFlightIds = inFlightUploadIdsRef.current;
    return () => {
      mountedRef.current = false;
      inFlightIds.clear();
    };
  }, []);

  const uploadingCount = uploadTasks.filter(
    (task) => task.status === "uploading",
  ).length;
  const failedUploadCount = uploadTasks.filter(
    (task) => task.status === "failed",
  ).length;
  const hasBlockingUploads = uploadTasks.length > 0;

  useEffect(() => {
    onBlockingUploadsChange?.(hasBlockingUploads);
  }, [hasBlockingUploads, onBlockingUploadsChange]);

  const didMountPendingSyncRef = useRef(false);
  useEffect(() => {
    // 仅在 uploadTasks 实际变化后的 effect 阶段回传父级，严禁在 setUploadTasks 更新函数内部触发父级 setState。
    if (!didMountPendingSyncRef.current) {
      didMountPendingSyncRef.current = true;
      return;
    }
    onPendingUploadsChange?.(
      uploadTasks.map((task) => ({
        file: task.file,
        requestId: task.requestId,
        reuseRequestId: task.reuseRequestId,
      })),
    );
  }, [onPendingUploadsChange, uploadTasks]);

  const sessionFiles = useMemo(() => {
    const flattened = (filesQuery.data?.pages ?? []).flatMap(
      (page) => page.items,
    );
    return Array.from(
      new Map(flattened.map((file) => [file.id, file])).values(),
    );
  }, [filesQuery.data?.pages]);

  const sessionFileMap = useMemo(() => {
    const map = new Map<string, SessionFile>();
    for (const file of sessionFiles) {
      map.set(file.id, file);
    }
    for (const file of Object.values(knownUploadedFiles)) {
      if (!map.has(file.id)) {
        map.set(file.id, file);
      }
    }
    return map;
  }, [knownUploadedFiles, sessionFiles]);

  const selectedFileIdSet = useMemo(
    () => new Set(selectedFileIds),
    [selectedFileIds],
  );

  const attachmentsDisabled =
    commandPending ||
    (isActive && !isWaitingInput && !isTargetedSubagentAnswer);
  const capabilityControlsDisabled =
    commandPending || isActive || uploadingCount > 0;
  const canSubmit =
    canSendWithoutDraft && !hasBlockingUploads && Boolean(draft.trim());

  useEffect(() => {
    if (attachmentsDisabled && attachmentMenuOpen) {
      setAttachmentMenuOpen(false);
    }
  }, [attachmentMenuOpen, attachmentsDisabled]);

  const runUploadTask = useCallback(
    async (task: SessionUploadTask, requestIdOverride?: string) => {
      if (inFlightUploadIdsRef.current.has(task.localId)) {
        return;
      }
      inFlightUploadIdsRef.current.add(task.localId);
      const requestId = requestIdOverride ?? task.requestId;
      try {
        const uploaded = await uploadMutation.mutateAsync({
          file: task.file,
          requestId,
        });
        if (!mountedRef.current) {
          return;
        }
        setKnownUploadedFiles((prev) => ({ ...prev, [uploaded.id]: uploaded }));
        setUploadTasks((prev) =>
          prev.filter((item) => item.localId !== task.localId),
        );
        // 通过 ref 读取最新已选列表，确保多文件同时上传完成时按序累加而不互相覆盖。
        const latestIds = selectedFileIdsRef.current;
        if (!latestIds.includes(uploaded.id)) {
          const nextIds = [...latestIds, uploaded.id];
          selectedFileIdsRef.current = nextIds;
          onChangeSelectedFileIds(nextIds);
        }
      } catch (error) {
        if (!mountedRef.current) {
          return;
        }
        const message = fileErrorMessage(error);
        const reusable = fileRequestCanBeReused(error);
        setUploadTasks((prev) =>
          prev.map((item) =>
            item.localId === task.localId
              ? {
                  ...item,
                  errorMessage: message,
                  requestId,
                  reuseRequestId: reusable,
                  status: "failed" as const,
                }
              : item,
          ),
        );
      } finally {
        inFlightUploadIdsRef.current.delete(task.localId);
      }
    },
    [onChangeSelectedFileIds, uploadMutation],
  );

  const handleAddFiles = useCallback(
    (incomingFiles: File[]) => {
      if (attachmentsDisabled || incomingFiles.length === 0) {
        return;
      }
      const nextErrors: string[] = [];
      const newTasks: SessionUploadTask[] = [];
      for (const file of incomingFiles) {
        const validationError = validateSessionUpload(file);
        if (validationError) {
          nextErrors.push(validationError);
          continue;
        }
        const requestId = newRequestId();
        newTasks.push({
          errorMessage: null,
          file,
          localId: requestId,
          requestId,
          reuseRequestId: false,
          status: "uploading",
        });
      }
      setValidationErrors(nextErrors);
      if (nextErrors.length > 0) {
        toast.error(nextErrors[0]);
      }
      if (newTasks.length > 0) {
        setUploadTasks((prev) => [...prev, ...newTasks]);
        // 选文件 / 拖入 / 粘贴后立即启动自动上传，无需用户再点击二次确认按钮。
        for (const task of newTasks) {
          void runUploadTask(task);
        }
      }
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    },
    [attachmentsDisabled, runUploadTask],
  );

  const retryUploadTask = useCallback(
    (localId: string) => {
      // 同步防重入校验：同一渲染帧内双击重试只放行一次真实上传请求。
      if (inFlightUploadIdsRef.current.has(localId)) {
        return;
      }
      const target = uploadTasksRef.current.find(
        (item) => item.localId === localId && item.status === "failed",
      );
      if (!target) {
        return;
      }
      // 网络失败或 5xx 等结果不确定时沿用原 Idempotency-Key；4xx 明确拒绝后换新键。
      const nextRequestId = target.reuseRequestId
        ? target.requestId
        : newRequestId();
      const updatedTask: SessionUploadTask = {
        ...target,
        errorMessage: null,
        requestId: nextRequestId,
        status: "uploading",
      };
      setUploadTasks((prev) =>
        prev.map((item) => (item.localId === localId ? updatedTask : item)),
      );
      void runUploadTask(updatedTask, nextRequestId);
    },
    [runUploadTask],
  );

  const removeUploadTask = useCallback((localId: string) => {
    setUploadTasks((prev) => prev.filter((item) => item.localId !== localId));
  }, []);

  const toggleExistingSessionFile = useCallback(
    (fileId: string) => {
      if (attachmentsDisabled) {
        return;
      }
      const current = selectedFileIdsRef.current;
      const next = current.includes(fileId)
        ? current.filter((id) => id !== fileId)
        : [...current, fileId];
      selectedFileIdsRef.current = next;
      onChangeSelectedFileIds(next);
    },
    [attachmentsDisabled, onChangeSelectedFileIds],
  );

  const attachmentChips = useMemo<ComposerAttachmentChip[]>(() => {
    const readyChips: ComposerAttachmentChip[] = selectedFileIds.map(
      (fileId) => {
        const file = sessionFileMap.get(fileId);
        const displayLabel = file
          ? fileDisplayName(file)
          : `文件 ${fileId.slice(0, 8)}`;
        const rawFilename = file ? file.filename : displayLabel;
        const canPreview = Boolean(
          file &&
            onPreviewFile &&
            canInlinePreviewFile(file.filename, file.mime_type),
        );
        return {
          displayName: displayLabel,
          extractedTextBytes: file?.extracted_text_bytes ?? null,
          extractionErrorCode: file?.extraction_error_code ?? null,
          extractionStatus: file?.extraction_status,
          // 保持原始 file.filename 供缩略图与安全白名单校验，展示文案使用 displayName。
          filename: rawFilename,
          id: `selected-${fileId}`,
          mimeType: file?.mime_type ?? null,
          onPreview:
            file && canPreview && onPreviewFile
              ? () => onPreviewFile(sessionFileToPreviewTarget(file))
              : undefined,
          onRemove: () => toggleExistingSessionFile(fileId),
          previewUrl:
            file && isPreviewableImageFile(file.filename, file.mime_type)
              ? sessionFilePreviewUrl(sessionId, file.id)
              : null,
          removeAriaLabel: `移除附件：${displayLabel}`,
          sizeBytes: file?.size_bytes ?? null,
          status: "ready",
        };
      },
    );

    const taskChips: ComposerAttachmentChip[] = uploadTasks.map((task) => ({
      errorMessage: task.errorMessage,
      filename: task.file.name,
      id: `upload-${task.localId}`,
      mimeType: task.file.type || null,
      onRemove:
        task.status === "failed"
          ? () => removeUploadTask(task.localId)
          : undefined,
      onRetry:
        task.status === "failed"
          ? () => retryUploadTask(task.localId)
          : undefined,
      removeAriaLabel: `移除待上传文件：${task.file.name}`,
      sizeBytes: task.file.size,
      status: task.status,
    }));

    return [...readyChips, ...taskChips];
  }, [
    onPreviewFile,
    removeUploadTask,
    retryUploadTask,
    selectedFileIds,
    sessionFileMap,
    sessionId,
    toggleExistingSessionFile,
    uploadTasks,
  ]);

  // 统计当前已附加/上传中的图片数量，用于驱动方案 A2 识图提示与单轮 5 张上限提醒。
  const imageAttachmentCount = useMemo(() => {
    let count = 0;
    for (const task of uploadTasks) {
      if (isImageAttachmentFile(task.file.name, task.file.type)) {
        count += 1;
      }
    }
    for (const fileId of selectedFileIds) {
      const file = sessionFileMap.get(fileId);
      if (file && isImageAttachmentFile(file.filename, file.mime_type)) {
        count += 1;
      }
    }
    return count;
  }, [selectedFileIds, sessionFileMap, uploadTasks]);
  const hasImageAttachment = imageAttachmentCount > 0;

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

  const submitCurrentDraft = async () => {
    const submittedDraft = draft;
    const accepted = await onSubmit(submittedDraft);
    if (accepted) {
      // 请求期间用户可能继续输入；仅清空仍与提交内容完全一致的草稿。
      setDraft((current) => (current === submittedDraft ? "" : current));
    }
  };

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
      <Popover onOpenChange={setAttachmentMenuOpen} open={attachmentMenuOpen}>
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
            <Upload aria-hidden="true" className="h-4 w-4 text-text-tertiary" />
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
            <p className="px-2 py-3 text-xs text-text-secondary" role="status">
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
                          {file.source === "artifact" ? "技能产物" : "用户上传"}
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
    <span className="font-medium text-brand">请补充信息以继续当前任务</span>
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
      onDismissValidationErrors={() => setValidationErrors([])}
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
  }),
);
