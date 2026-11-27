import {
  AlertTriangle,
  ArrowUp,
  Eye,
  Loader2,
  Lock,
  Square,
  Upload,
} from "lucide-react";
import {
  memo,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";

import {
  AutoResizeTextarea,
  Button,
  InlineAlert,
  cn,
  toast,
} from "../../shared/ui";
import { MAX_VISION_IMAGES_PER_RUN } from "../files/model";
import {
  resolveEffectiveUserModel,
  useUserModels,
  userModelDisplayLabel,
} from "../models";
import { SkillCatalogPicker } from "../skills/SkillCatalogPicker";
import {
  ComposerAttachmentBar,
  type ComposerAttachmentChip,
  type ComposerSkillChip,
} from "./ComposerAttachmentBar";
import { KnowledgeScopePopover } from "./KnowledgeScopePopover";
import { ModelSelectorPopover } from "./ModelSelectorPopover";

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
export const ChatComposer = memo(function ChatComposer({
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
  const resolvedImageCount =
    imageAttachmentCount ?? (hasImageAttachment ? 1 : 0);
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
});
