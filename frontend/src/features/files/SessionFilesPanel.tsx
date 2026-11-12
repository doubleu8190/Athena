import { useEffect, useId, useRef, useState } from "react";

import { formatDateTime } from "../../shared/lib/format";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { fileErrorMessage } from "./errorMessages";
import { useSessionFiles, useUploadSessionFileMutation } from "./hooks";
import {
  fileDisplayName,
  validateSessionUpload,
  type PendingSessionUpload,
  type SessionFile,
} from "./model";
import { SessionFileDownloadButton } from "./SessionFileDownloadButton";
import { newRequestId } from "../chat/requestId";

function formatBytes(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function fileRequestCanBeReused(error: unknown): boolean {
  if (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    "detail" in error
  ) {
    const status = Number(error.status);
    const detail = String(error.detail);
    return status === 0 || status >= 500 || detail === "invalid_response";
  }
  return false;
}

interface SessionFilesPanelProps {
  disabled?: boolean;
  initialPendingUploads?: PendingSessionUpload[];
  onChangeSelectedFileIds: (fileIds: string[]) => void;
  onBusyChange?: (busy: boolean) => void;
  onInitialPendingUploadsChange?: (
    uploads: PendingSessionUpload[],
  ) => void;
  selectedFileIds: ReadonlySet<string>;
  sessionId: string;
}

export function SessionFilesPanel({
  disabled = false,
  initialPendingUploads = [],
  onChangeSelectedFileIds,
  onBusyChange,
  onInitialPendingUploadsChange,
  selectedFileIds,
  sessionId,
}: SessionFilesPanelProps) {
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const files = useSessionFiles(sessionId);
  const upload = useUploadSessionFileMutation(sessionId);
  const [pendingUploads, setPendingUploads] = useState<
    PendingSessionUpload[]
  >(initialPendingUploads);
  const [fileValidationErrors, setFileValidationErrors] = useState<
    string[]
  >([]);
  const [uploadErrors, setUploadErrors] = useState<Record<string, string>>(
    {},
  );
  const [uploadNotice, setUploadNotice] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);

  useEffect(() => {
    onBusyChange?.(uploading);
  }, [onBusyChange, uploading]);

  const items = (files.data?.pages ?? []).flatMap((page) => page.items);
  const uniqueItems = Array.from(
    new Map(items.map((file) => [file.id, file])).values(),
  );

  const replaceFileSelection = (fileId: string, selected: boolean) => {
    const next = new Set(selectedFileIds);
    if (selected) {
      next.add(fileId);
    } else {
      next.delete(fileId);
    }
    onChangeSelectedFileIds(Array.from(next));
  };

  const addFiles = (selected: FileList | null) => {
    if (!selected) {
      return;
    }
    const errors: string[] = [];
    const accepted: PendingSessionUpload[] = [];
    Array.from(selected).forEach((file) => {
      const validationError = validateSessionUpload(file);
      if (validationError) {
        errors.push(validationError);
        return;
      }
      accepted.push({
        file,
        requestId: newRequestId(),
        reuseRequestId: false,
      });
    });
    setFileValidationErrors(errors);
    setUploadNotice(null);
    if (accepted.length > 0) {
      const next = [...pendingUploads, ...accepted];
      setPendingUploads(next);
      onInitialPendingUploadsChange?.(next);
    }
    if (inputRef.current) {
      inputRef.current.value = "";
    }
  };

  const removePendingUpload = (requestId: string) => {
    if (uploading) {
      return;
    }
    const next = pendingUploads.filter(
      (item) => item.requestId !== requestId,
    );
    setPendingUploads(next);
    onInitialPendingUploadsChange?.(next);
    setUploadErrors((current) => {
      const next = { ...current };
      delete next[requestId];
      return next;
    });
  };

  const uploadPendingFiles = async () => {
    if (uploading || pendingUploads.length === 0) {
      return;
    }
    setUploading(true);
    setUploadNotice(null);
    setFileValidationErrors([]);
    const remaining: PendingSessionUpload[] = [];
    const uploadedIds: string[] = [];
    try {
      for (const pending of pendingUploads) {
        // 已明确失败的请求在再次操作时换新键；网络/5xx 等结果不明时沿用原键。
        const requestId =
          uploadErrors[pending.requestId] && !pending.reuseRequestId
            ? newRequestId()
            : pending.requestId;
        try {
          const uploaded = await upload.mutateAsync({
            file: pending.file,
            requestId,
          });
          uploadedIds.push(uploaded.id);
          setUploadErrors((current) => {
            const next = { ...current };
            delete next[pending.requestId];
            delete next[requestId];
            return next;
          });
        } catch (error) {
          // 网络失败或 5xx 无法断言服务端未提交，按原请求标识重试避免重复文件。
          remaining.push({
            ...pending,
            requestId,
            reuseRequestId: fileRequestCanBeReused(error),
          });
          setUploadErrors((current) => {
            const next = { ...current };
            delete next[pending.requestId];
            next[requestId] = fileErrorMessage(error);
            return next;
          });
        }
      }
      setPendingUploads(remaining);
      onInitialPendingUploadsChange?.(remaining);
      if (uploadedIds.length > 0) {
        const nextSelection = new Set(selectedFileIds);
        uploadedIds.forEach((fileId) => nextSelection.add(fileId));
        onChangeSelectedFileIds(Array.from(nextSelection));
        setUploadNotice(
          `已发布 ${uploadedIds.length} 个文件，并选中用于下一次 Run。`,
        );
        await files.refetch();
      }
    } finally {
      setUploading(false);
    }
  };

  const toggleExistingFile = (file: SessionFile) => {
    replaceFileSelection(file.id, !selectedFileIds.has(file.id));
  };

  return (
    <section
      aria-busy={files.isPending || uploading}
      aria-labelledby={`${inputId}-title`}
      className="session-files-panel"
    >
      <header className="session-files-heading">
        <div>
          <p className="empty-panel-kicker">Session files</p>
          <h2 id={`${inputId}-title`}>会话文件</h2>
          <p>
            上传或勾选已发布文件后，文件 ID 会随下一次提交/补充回答固定到 Run。
          </p>
        </div>
        <button
          className="button-quiet"
          disabled={disabled || uploading}
          onClick={() => inputRef.current?.click()}
          type="button"
        >
          <Icon name="plus" size={14} />
          选择文件
        </button>
        <input
          aria-label="选择会话文件"
          className="sr-only"
          disabled={disabled || uploading}
          id={inputId}
          multiple
          onChange={(event) => addFiles(event.target.files)}
          ref={inputRef}
          type="file"
        />
      </header>

      {pendingUploads.length > 0 ? (
        <div className="session-file-pending">
          <div className="session-file-pending-heading">
            <span>待上传 {pendingUploads.length} 个文件</span>
            <button
              className="button-primary"
              disabled={disabled || uploading}
              onClick={() => void uploadPendingFiles()}
              type="button"
            >
              {uploading ? "正在上传…" : "上传并选中"}
            </button>
          </div>
          <ul>
            {pendingUploads.map((pending) => (
              <li key={pending.requestId}>
                <span>
                  <strong>{pending.file.name}</strong>
                  <small>{formatBytes(pending.file.size)}</small>
                </span>
                {uploadErrors[pending.requestId] ? (
                  <span className="session-file-inline-error" role="alert">
                    {uploadErrors[pending.requestId]}
                  </span>
                ) : null}
                <button
                  aria-label={`移除待上传文件：${pending.file.name}`}
                  className="text-button"
                  disabled={uploading}
                  onClick={() => removePendingUpload(pending.requestId)}
                  type="button"
                >
                  移除
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {uploadNotice ? (
        <p className="session-file-notice" role="status">
          {uploadNotice}
        </p>
      ) : null}

      {fileValidationErrors.length > 0 ? (
        <ul className="session-file-validation" role="alert">
          {fileValidationErrors.map((error, index) => (
            <li key={`${index}-${error}`}>{error}</li>
          ))}
        </ul>
      ) : null}

      {files.isPending ? (
        <p className="session-file-state" role="status">
          正在读取会话文件…
        </p>
      ) : files.isError ? (
        <ErrorBanner
          message={fileErrorMessage(files.error)}
          onRetry={() => void files.refetch()}
        />
      ) : uniqueItems.length === 0 ? (
        <EmptyState
          compact
          description="会话上传文件与技能成功产物都会在发布后出现在这里。"
          icon="folder"
          title="还没有已发布文件"
        />
      ) : (
        <>
          <ul className="session-file-list">
            {uniqueItems.map((file) => {
              const selected = selectedFileIds.has(file.id);
              return (
                <li
                  className={selected ? "is-selected" : ""}
                  key={file.id}
                >
                  <label className="workspace-checkbox session-file-select">
                    <input
                      checked={selected}
                      disabled={disabled || uploading}
                      onChange={() => toggleExistingFile(file)}
                      type="checkbox"
                    />
                    <span>
                      <strong>{fileDisplayName(file)}</strong>
                      <small>
                        {file.filename} · {formatBytes(file.size_bytes)} ·{" "}
                        {file.source === "artifact" ? "技能产物" : "用户上传"}
                      </small>
                    </span>
                  </label>
                  <span className="session-file-meta">
                    {file.mime_type} · {formatDateTime(file.created_at)}
                  </span>
                  <SessionFileDownloadButton
                    compact
                    fileId={file.id}
                    filename={fileDisplayName(file)}
                    sessionId={sessionId}
                  />
                </li>
              );
            })}
          </ul>
          {files.hasNextPage ? (
            <button
              className="button-quiet session-file-more"
              disabled={files.isFetchingNextPage}
              onClick={() => void files.fetchNextPage()}
              type="button"
            >
              {files.isFetchingNextPage
                ? "正在加载…"
                : "加载更多会话文件"}
            </button>
          ) : null}
        </>
      )}
    </section>
  );
}
