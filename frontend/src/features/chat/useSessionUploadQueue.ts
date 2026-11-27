import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";

import { newRequestId } from "../../shared/lib/requestId";
import { toast } from "../../shared/ui";
import {
  fileErrorMessage,
  fileRequestCanBeReused,
} from "../files/errorMessages";
import { useSessionFiles, useUploadSessionFileMutation } from "../files/hooks";
import {
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
import {
  isImageAttachmentFile,
  type ComposerAttachmentChip,
} from "./ComposerAttachmentBar";

export interface SessionUploadTask {
  localId: string;
  file: File;
  requestId: string;
  reuseRequestId: boolean;
  status: "uploading" | "failed";
  errorMessage: string | null;
}

export interface UseSessionUploadQueueOptions {
  attachmentsDisabled: boolean;
  initialPendingUploads?: PendingSessionUpload[];
  onBlockingUploadsChange?: (hasBlockingUploads: boolean) => void;
  onChangeSelectedFileIds: (fileIds: string[]) => void;
  onPendingUploadsChange?: (uploads: PendingSessionUpload[]) => void;
  onPreviewFile?: (target: FilePreviewTarget) => void;
  selectedFileIds: string[];
  sessionId: string;
}

export interface UseSessionUploadQueueResult {
  attachmentChips: ComposerAttachmentChip[];
  dismissValidationErrors: () => void;
  failedUploadCount: number;
  fileInputRef: RefObject<HTMLInputElement>;
  filesQuery: ReturnType<typeof useSessionFiles>;
  handleAddFiles: (incomingFiles: File[]) => void;
  hasBlockingUploads: boolean;
  hasImageAttachment: boolean;
  imageAttachmentCount: number;
  selectedFileIdSet: ReadonlySet<string>;
  sessionFiles: SessionFile[];
  toggleExistingSessionFile: (fileId: string) => void;
  uploadingCount: number;
  validationErrors: readonly string[];
}

/**
 * 会话输入区附件上传队列与会话文件引用状态管理：
 * - 承接草稿移交的失败待传文件，保留各自的 Idempotency-Key 复用策略；
 * - 维护并发防重入锁（inFlightUploadIdsRef）与多文件完成累加选中的 ref 边界；
 * - 暴露稳定引用句柄，避免触发 memo 化容器或输入表单无效重渲染。
 */
export function useSessionUploadQueue({
  attachmentsDisabled,
  initialPendingUploads = [],
  onBlockingUploadsChange,
  onChangeSelectedFileIds,
  onPendingUploadsChange,
  onPreviewFile,
  selectedFileIds,
  sessionId,
}: UseSessionUploadQueueOptions): UseSessionUploadQueueResult {
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

  const fileInputRef = useRef<HTMLInputElement>(null);
  const mountedRef = useRef(true);
  const selectedFileIdsRef = useRef(selectedFileIds);
  selectedFileIdsRef.current = selectedFileIds;
  const uploadTasksRef = useRef(uploadTasks);
  uploadTasksRef.current = uploadTasks;
  // 同步防重入锁：记录当前正在上传中的任务 localId，彻底杜绝快速双击“重试”导致同文件并发双传。
  const inFlightUploadIdsRef = useRef<Set<string>>(new Set());

  const filesQuery = useSessionFiles(sessionId, Boolean(sessionId));
  const { mutateAsync: uploadSessionFile } =
    useUploadSessionFileMutation(sessionId);

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

  const runUploadTask = useCallback(
    async (task: SessionUploadTask, requestIdOverride?: string) => {
      if (inFlightUploadIdsRef.current.has(task.localId)) {
        return;
      }
      inFlightUploadIdsRef.current.add(task.localId);
      const requestId = requestIdOverride ?? task.requestId;
      try {
        const uploaded = await uploadSessionFile({
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
    [onChangeSelectedFileIds, uploadSessionFile],
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

  const dismissValidationErrors = useCallback(() => {
    setValidationErrors([]);
  }, []);

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

  return {
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
  };
}
