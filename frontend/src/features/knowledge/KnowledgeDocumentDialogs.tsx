import { useEffect, useId, useRef, useState } from "react";

import { formatDateTime } from "../../shared/lib/format";
import { newRequestId } from "../../shared/lib/requestId";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { EmptyState } from "../../shared/ui/EmptyState";
import { Modal } from "../../shared/ui/Modal";
import { WorkspaceFilePicker } from "../../shared/ui/WorkspaceFilePicker";
import {
  idempotencyKeyCanBeReused,
  knowledgeErrorMessage,
  versionErrorMessage,
} from "./errorMessages";
import { knowledgeWarningLabel } from "./display";
import {
  useDeleteKnowledgeDocumentMutation,
  useImportKnowledgeTextMutation,
  useKnowledgeVersions,
  useRenameKnowledgeDocumentMutation,
  useReplaceKnowledgeFileMutation,
  useReplaceKnowledgeTextMutation,
  useRetryKnowledgeVersionMutation,
  useUploadKnowledgeDocumentMutation,
} from "./hooks";
import { KnowledgeSourceButton } from "./KnowledgeSourceButton";
import { KnowledgeStateBadge } from "./KnowledgeStateBadge";
import {
  defaultDocumentTitle,
  KNOWLEDGE_FILE_ACCEPT,
  normalizeDocumentTitle,
  validateDocumentTitle,
  validateKnowledgeFile,
  validateKnowledgeText,
  type KnowledgeDocument,
  type KnowledgeVersion,
} from "./model";

type IngestMode = "upload" | "text";

interface KnowledgeIngestDialogProps {
  baseId: string;
  document?: KnowledgeDocument;
  onClose: () => void;
  onImported?: () => void;
  open: boolean;
}

export function KnowledgeIngestDialog({
  baseId,
  document,
  onClose,
  onImported,
  open,
}: KnowledgeIngestDialogProps) {
  const formId = useId();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const titleErrorId = useId();
  const textErrorId = useId();
  const fileErrorId = useId();
  const fileHintId = useId();
  const [mode, setMode] = useState<IngestMode>("upload");
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [titleError, setTitleError] = useState<string | null>(null);
  const [textError, setTextError] = useState<string | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [idempotencyKey, setIdempotencyKey] = useState(newRequestId);
  const importText = useImportKnowledgeTextMutation(baseId);
  const upload = useUploadKnowledgeDocumentMutation(baseId);
  const replaceText = useReplaceKnowledgeTextMutation(
    baseId,
    document?.id ?? "",
  );
  const replaceFile = useReplaceKnowledgeFileMutation(
    baseId,
    document?.id ?? "",
  );
  const pending =
    importText.isPending ||
    upload.isPending ||
    replaceText.isPending ||
    replaceFile.isPending;
  const replacing = Boolean(document);

  useEffect(() => {
    if (open) {
      setMode("upload");
      setFile(null);
      setTitle("");
      setText("");
      setTitleError(null);
      setTextError(null);
      setFileError(null);
      setRequestError(null);
      setIdempotencyKey(newRequestId());
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  }, [open, document?.id]);

  const close = () => {
    if (!pending) {
      onClose();
    }
  };

  const rotateIdempotencyKey = () => {
    setIdempotencyKey(newRequestId());
  };

  const selectMode = (next: IngestMode) => {
    if (pending) {
      return;
    }
    if (mode !== next) {
      setMode(next);
      setTitleError(null);
      setTextError(null);
      setFileError(null);
      setRequestError(null);
      rotateIdempotencyKey();
    }
  };

  const handleFile = (nextFile: File | null) => {
    if (pending) {
      return;
    }
    setFile(nextFile);
    setFileError(null);
    setRequestError(null);
    rotateIdempotencyKey();
    if (nextFile && !replacing) {
      setTitle((current) =>
        current.trim()
          ? current
          : defaultDocumentTitle(nextFile.name),
      );
    }
  };

  const handleSubmit = async () => {
    const nextTitleError = replacing
      ? null
      : validateDocumentTitle(title);
    const nextTextError =
      mode === "text" ? validateKnowledgeText(text) : null;
    const nextFileError =
      mode === "upload" ? (file ? validateKnowledgeFile(file) : "请选择文件。") : null;
    setTitleError(nextTitleError);
    setTextError(nextTextError);
    setFileError(nextFileError);
    if (nextTitleError || nextTextError || nextFileError) {
      return;
    }

    setRequestError(null);
    try {
      if (mode === "text") {
        if (replacing && document) {
          await replaceText.mutateAsync({
            idempotencyKey,
            title: document.title,
            text,
          });
        } else {
          await importText.mutateAsync({
            idempotencyKey,
            title: normalizeDocumentTitle(title),
            text,
          });
        }
      } else if (file) {
        if (replacing && document) {
          await replaceFile.mutateAsync({ file, idempotencyKey });
        } else {
          await upload.mutateAsync({
            file,
            idempotencyKey,
            title: normalizeDocumentTitle(title),
          });
        }
      }
      onClose();
      onImported?.();
    } catch (failure) {
      setRequestError(knowledgeErrorMessage(failure));
      if (!idempotencyKeyCanBeReused(failure)) {
        rotateIdempotencyKey();
      }
    }
  };

  return (
    <Modal
      closeDisabled={pending}
      description={
        replacing
          ? "新版本处理成功前，当前活动版本仍可检索。"
          : "资料提交后由后端后台解析和建立索引。"
      }
      footer={
        <>
          <button
            className="button-quiet"
            disabled={pending}
            onClick={close}
            type="button"
          >
            取消
          </button>
          <button
            className="button-primary"
            disabled={pending}
            form={formId}
            type="submit"
          >
            {pending
              ? replacing
                ? "正在提交新版本…"
                : "正在提交资料…"
              : replacing
                ? "提交新版本"
                : "添加资料"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      title={replacing ? "添加文档新版本" : "添加资料"}
    >
      <form
        className="knowledge-form"
        id={formId}
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        {replacing && document ? (
          <p className="knowledge-form-note">
            文档：<strong>{document.title}</strong>
          </p>
        ) : null}

        <div
          aria-label="资料提交方式"
          className="knowledge-mode-switch"
          role="group"
        >
          <button
            aria-pressed={mode === "upload"}
            disabled={pending}
            onClick={() => selectMode("upload")}
            type="button"
          >
            上传文件
          </button>
          <button
            aria-pressed={mode === "text"}
            disabled={pending}
            onClick={() => selectMode("text")}
            type="button"
          >
            粘贴文本
          </button>
        </div>

        {!replacing ? (
          <label className="workspace-field" htmlFor={`${formId}-title`}>
            <span>文档标题</span>
            <input
              aria-label="文档标题"
              aria-describedby={titleError ? titleErrorId : undefined}
              aria-invalid={Boolean(titleError)}
              autoComplete="off"
              data-autofocus
              disabled={pending}
              id={`${formId}-title`}
              onChange={(event) => {
                setTitle(event.target.value);
                setTitleError(null);
                setRequestError(null);
                rotateIdempotencyKey();
              }}
              placeholder="用于在文档列表中识别这份资料"
              value={title}
            />
            <small>最多 255 个字符。</small>
          </label>
        ) : null}
        {titleError ? (
          <p className="field-error" id={titleErrorId} role="alert">
            {titleError}
          </p>
        ) : null}

        {mode === "upload" ? (
          <div className="workspace-field">
            <span>{replacing ? "新版本文件" : "文件"}</span>
            <WorkspaceFilePicker
              accept={KNOWLEDGE_FILE_ACCEPT}
              describedBy={
                fileError ? `${fileHintId} ${fileErrorId}` : fileHintId
              }
              disabled={pending}
              file={file}
              id={`${formId}-file`}
              inputRef={fileInputRef}
              invalid={Boolean(fileError)}
              label={replacing ? "新版本文件" : "文件"}
              onChange={handleFile}
              placeholder={
                replacing ? "未选择新版本文件" : "未选择知识文档"
              }
              triggerLabel="选择文档文件"
            />
            <small id={fileHintId}>
              支持 TXT、MD、PDF、DOCX、XLSX、PPTX、PNG、JPG、JPEG，最大 20 MiB。
            </small>
          </div>
        ) : (
          <label className="workspace-field" htmlFor={`${formId}-text`}>
            <span>文本内容</span>
            <textarea
              aria-label="文本内容"
              aria-describedby={textError ? textErrorId : undefined}
              aria-invalid={Boolean(textError)}
              disabled={pending}
              id={`${formId}-text`}
              onChange={(event) => {
                setText(event.target.value);
                setTextError(null);
                setRequestError(null);
                rotateIdempotencyKey();
              }}
              placeholder="粘贴需要解析和检索的纯文本"
              rows={9}
              value={text}
            />
            <small>最多 1,000,000 个字符。</small>
          </label>
        )}
        {fileError ? (
          <p className="field-error" id={fileErrorId} role="alert">
            {fileError}
          </p>
        ) : null}
        {textError ? (
          <p className="field-error" id={textErrorId} role="alert">
            {textError}
          </p>
        ) : null}
        {pending ? (
          <p className="knowledge-form-status" role="status">
            正在向后端提交资料，请勿重复操作。
          </p>
        ) : null}
        {requestError ? <ErrorBanner message={requestError} /> : null}
      </form>
    </Modal>
  );
}

interface RenameKnowledgeDocumentDialogProps {
  baseId: string;
  document: KnowledgeDocument;
  onClose: () => void;
  open: boolean;
}

export function RenameKnowledgeDocumentDialog({
  baseId,
  document,
  onClose,
  open,
}: RenameKnowledgeDocumentDialogProps) {
  const formId = useId();
  const errorId = useId();
  const [title, setTitle] = useState(document.title);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useRenameKnowledgeDocumentMutation(baseId);
  const { reset: resetMutation } = mutation;

  useEffect(() => {
    if (open) {
      setTitle(document.title);
      setValidationError(null);
      setRequestError(null);
      resetMutation();
    }
  }, [document.title, open, resetMutation]);

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const handleSubmit = async () => {
    const validation = validateDocumentTitle(title);
    setValidationError(validation);
    if (validation) {
      return;
    }
    const normalized = normalizeDocumentTitle(title);
    if (normalized === document.title) {
      onClose();
      return;
    }
    setRequestError(null);
    try {
      await mutation.mutateAsync({
        documentId: document.id,
        title: normalized,
      });
      onClose();
    } catch (failure) {
      setRequestError(knowledgeErrorMessage(failure));
    }
  };

  return (
    <Modal
      closeDisabled={mutation.isPending}
      footer={
        <>
          <button
            className="button-quiet"
            disabled={mutation.isPending}
            onClick={close}
            type="button"
          >
            取消
          </button>
          <button
            className="button-primary"
            disabled={mutation.isPending}
            form={formId}
            type="submit"
          >
            {mutation.isPending ? "正在保存…" : "保存标题"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      title="重命名文档"
    >
      <form
        id={formId}
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <label className="workspace-field" htmlFor={`${formId}-title`}>
          <span>文档标题</span>
          <input
            aria-label="文档标题"
            aria-describedby={validationError ? errorId : undefined}
            aria-invalid={Boolean(validationError)}
            autoComplete="off"
            data-autofocus
            id={`${formId}-title`}
            onChange={(event) => {
              setTitle(event.target.value);
              setValidationError(null);
              setRequestError(null);
            }}
            value={title}
          />
        </label>
        {validationError ? (
          <p className="field-error" id={errorId} role="alert">
            {validationError}
          </p>
        ) : null}
        {requestError ? <ErrorBanner message={requestError} /> : null}
      </form>
    </Modal>
  );
}

interface DeleteKnowledgeDocumentDialogProps {
  baseId: string;
  document: KnowledgeDocument;
  onClose: () => void;
  onDeleted: () => void;
  open: boolean;
}

export function DeleteKnowledgeDocumentDialog({
  baseId,
  document,
  onClose,
  onDeleted,
  open,
}: DeleteKnowledgeDocumentDialogProps) {
  const checkboxId = useId();
  const [confirmed, setConfirmed] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useDeleteKnowledgeDocumentMutation(baseId);
  const { reset: resetMutation } = mutation;

  useEffect(() => {
    if (open) {
      setConfirmed(false);
      setRequestError(null);
      resetMutation();
    }
  }, [open, resetMutation]);

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const handleDelete = async () => {
    if (!confirmed) {
      return;
    }
    setRequestError(null);
    try {
      await mutation.mutateAsync(document.id);
      onClose();
      onDeleted();
    } catch (failure) {
      setRequestError(knowledgeErrorMessage(failure));
    }
  };

  return (
    <Modal
      closeDisabled={mutation.isPending}
      description="文档版本和检索片段将不再可访问。"
      footer={
        <>
          <button
            className="button-quiet"
            data-autofocus
            disabled={mutation.isPending}
            onClick={close}
            type="button"
          >
            取消
          </button>
          <button
            className="button-danger"
            disabled={!confirmed || mutation.isPending}
            onClick={() => void handleDelete()}
            type="button"
          >
            {mutation.isPending ? "正在删除…" : "删除文档"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      title="删除文档"
    >
      <p className="confirm-copy">
        确定删除“{document.title}”吗？删除后无法继续检索或下载其来源。
      </p>
      <label
        className="workspace-checkbox workspace-checkbox--danger"
        htmlFor={checkboxId}
      >
        <input
          checked={confirmed}
          id={checkboxId}
          onChange={(event) => setConfirmed(event.target.checked)}
          type="checkbox"
        />
        <span>我已确认要删除这个文档</span>
      </label>
      {requestError ? <ErrorBanner message={requestError} /> : null}
    </Modal>
  );
}

interface VersionHistoryDialogProps {
  baseId: string;
  /** 是否拥有知识库管理写权限；默认 false 保持 fail-closed，避免只读用户看到重试等写操作。 */
  canManage?: boolean;
  document: KnowledgeDocument;
  onClose: () => void;
  open: boolean;
}

function VersionRow({
  baseId,
  canRetry,
  documentId,
  retry,
  version,
}: {
  baseId: string;
  canRetry: boolean;
  documentId: string;
  retry: (versionId: string) => void;
  version: KnowledgeVersion;
}) {
  return (
    <article className="knowledge-version-row">
      <div className="knowledge-version-heading">
        <div>
          <strong>版本 {formatDateTime(version.created_at)}</strong>
          <span>
            {version.completed_at
              ? `完成于 ${formatDateTime(version.completed_at)}`
              : "尚未完成"}
          </span>
        </div>
        <KnowledgeStateBadge state={version.state} />
      </div>
      <dl className="knowledge-version-meta">
        <div>
          <dt>检索片段</dt>
          <dd>{version.state === "ready" ? version.chunk_count : "—"}</dd>
        </div>
        <div>
          <dt>警告</dt>
          <dd>{version.warnings.length}</dd>
        </div>
      </dl>
      {version.error_code ? (
        <p className="knowledge-version-error" role="alert">
          {versionErrorMessage(version.error_code)}
          <code>{version.error_code}</code>
        </p>
      ) : null}
      {version.warnings.length > 0 ? (
        <ul className="knowledge-warning-list">
          {version.warnings.map((warning) => (
            <li key={warning}>{knowledgeWarningLabel(warning)}</li>
          ))}
        </ul>
      ) : null}
      <div className="knowledge-version-actions">
        <KnowledgeSourceButton
          baseId={baseId}
          compact
          documentId={documentId}
          versionId={version.id}
        />
        {canRetry && version.state === "failed" ? (
          <button
            className="button-quiet"
            onClick={() => retry(version.id)}
            type="button"
          >
            重试此版本
          </button>
        ) : null}
      </div>
    </article>
  );
}

export function VersionHistoryDialog({
  baseId,
  canManage = false,
  document,
  onClose,
  open,
}: VersionHistoryDialogProps) {
  const versions = useKnowledgeVersions(baseId, document.id);
  const retryMutation = useRetryKnowledgeVersionMutation(
    baseId,
    document.id,
  );
  const latest = versions.data?.[0] ?? null;
  // 仅当具备管理写权限且当前无排队中版本时，才允许对最新失败版本发起重试。
  const canRetry =
    canManage &&
    document.pending_version_id === null &&
    latest?.state === "failed";

  return (
    <Modal
      description="版本按后端返回时间倒序排列，来源下载仍经过鉴权接口。"
      footer={
        <button className="button-quiet" onClick={onClose} type="button">
          关闭
        </button>
      }
      onClose={onClose}
      open={open}
      panelClassName="knowledge-version-dialog"
      title="版本历史"
    >
      {versions.isPending ? (
        <p className="knowledge-form-status" role="status">
          正在读取版本…
        </p>
      ) : versions.isError ? (
        <ErrorBanner
          message={knowledgeErrorMessage(versions.error)}
          onRetry={() => void versions.refetch()}
        />
      ) : versions.data.length === 0 ? (
        <EmptyState
          compact
          description="后端没有返回该文档的版本记录。"
          icon="book"
          title="暂无版本"
        />
      ) : (
        <div className="knowledge-version-list">
          {versions.data.map((version) => (
            <VersionRow
              baseId={baseId}
              canRetry={canRetry && version.id === latest?.id}
              documentId={document.id}
              key={version.id}
              retry={(versionId) => retryMutation.mutate(versionId)}
              version={version}
            />
          ))}
        </div>
      )}
      {retryMutation.isPending ? (
        <p className="knowledge-form-status" role="status">
          正在重新排队失败版本…
        </p>
      ) : null}
      {retryMutation.isSuccess ? (
        <p className="knowledge-form-status" role="status">
          版本已重新排队，状态会自动刷新。
        </p>
      ) : null}
      {retryMutation.isError ? (
        <ErrorBanner message={knowledgeErrorMessage(retryMutation.error)} />
      ) : null}
    </Modal>
  );
}
