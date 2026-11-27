import { Check, Code2, Copy, Eye, RefreshCw, Table2 } from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { copyTextToClipboard } from "../../shared/lib/clipboard";
import { formatBytes } from "../../shared/lib/format";
import {
  Button,
  DialogBody,
  InlineAlert,
  Skeleton,
  cn,
  toast,
} from "../../shared/ui";
import { MarkdownContent } from "../../shared/ui/MarkdownContent";
import { getSessionFileContent } from "./api";
import {
  extractionErrorSummary,
  fileErrorCode,
  fileErrorMessage,
  isDocumentExtractionErrorCode,
} from "./errorMessages";
import { FileCsvPreview } from "./FileCsvPreview";
import {
  SESSION_FILE_CONTENT_DEFAULT_LIMIT_BYTES,
  type FilePreviewKind,
  type FilePreviewTarget,
  type SessionFile,
  type SessionFileContentSourceKind,
} from "./model";

const COPY_FEEDBACK_MS = 2_000;

export interface FileTextPreviewMeta {
  eof: boolean;
  fileId: string | null;
  hasLoadedTextContent: boolean;
  isExtractedDocument: boolean;
  isJsonPretty: boolean;
  resolvedFile: SessionFile | null;
}

export const EMPTY_FILE_TEXT_PREVIEW_META: FileTextPreviewMeta = {
  eof: false,
  fileId: null,
  hasLoadedTextContent: false,
  isExtractedDocument: false,
  isJsonPretty: false,
  resolvedFile: null,
};

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

// JSON 仅在完整加载（eof === true）且语法合法时自动格式化，避免截断分片强行解析报错。
function formatJsonPreview(
  previewKind: FilePreviewKind,
  eof: boolean,
  contentText: string,
): { text: string; pretty: boolean } | null {
  if (previewKind !== "json") {
    return null;
  }
  if (!eof || !contentText.trim()) {
    return { pretty: false, text: contentText };
  }
  try {
    const parsed: unknown = JSON.parse(contentText);
    return { pretty: true, text: JSON.stringify(parsed, null, 2) };
  } catch {
    return { pretty: false, text: contentText };
  }
}

export interface FileTextPreviewProps {
  file: FilePreviewTarget;
  onExtractionFailed?: () => void;
  onFileMetadataUpdated?: (file: SessionFile) => void;
  onMetaChange?: (meta: FileTextPreviewMeta) => void;
  open: boolean;
  previewKind: Exclude<FilePreviewKind, "image" | "unsupported">;
  sessionId: string;
}

/**
 * 文本类文件（Markdown / CSV / JSON / 纯文本 / 办公文档提取文本）在线预览：
 * - 按后端 next_offset 续读分页（直至 eof === true），并通过请求序号与 AbortController 防止切文件串染；
 * - 承接渲染/源码视图切换、一键复制反馈、JSON 完整格式化与提取失败温和降级提示。
 */
export function FileTextPreview({
  file,
  onExtractionFailed,
  onFileMetadataUpdated,
  onMetaChange,
  open,
  previewKind,
  sessionId,
}: FileTextPreviewProps) {
  const [contentText, setContentText] = useState("");
  const [sourceKind, setSourceKind] =
    useState<SessionFileContentSourceKind | null>(null);
  const [nextOffset, setNextOffset] = useState(0);
  const [eof, setEof] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [resolvedFile, setResolvedFile] = useState<SessionFile | null>(null);
  const [loadingInitial, setLoadingInitial] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [initialError, setInitialError] = useState<unknown>(null);
  const [loadMoreError, setLoadMoreError] = useState<unknown>(null);
  const [viewMode, setViewMode] = useState<"rendered" | "raw">("rendered");
  const [copied, setCopied] = useState(false);
  const [reloadAttempt, setReloadAttempt] = useState(0);

  // 递增请求序号、正文镜像与在途 AbortController 共同保证切文件或关弹窗时零串染。
  const requestSeqRef = useRef(0);
  const activeAbortRef = useRef<AbortController | null>(null);
  const loadingMoreRef = useRef(false);
  const contentTextRef = useRef("");
  const copyTimerRef = useRef<number | null>(null);
  const onFileMetadataUpdatedRef = useRef(onFileMetadataUpdated);
  onFileMetadataUpdatedRef.current = onFileMetadataUpdated;
  const onExtractionFailedRef = useRef(onExtractionFailed);
  onExtractionFailedRef.current = onExtractionFailed;
  const onMetaChangeRef = useRef(onMetaChange);
  onMetaChangeRef.current = onMetaChange;

  const abortInFlight = useCallback(() => {
    if (activeAbortRef.current) {
      activeAbortRef.current.abort();
      activeAbortRef.current = null;
    }
    loadingMoreRef.current = false;
  }, []);

  const fileId = file.fileId;
  const extractionStatus = file.extractionStatus;
  const extractionErrorCode = file.extractionErrorCode ?? null;

  // 已知文档提取失败（且尚未重新拉取）时直接展示友好降级卡片，避免发起必败的 422 请求。
  const knownExtractionFailed =
    previewKind === "document" && extractionStatus === "failed";

  useEffect(() => {
    return () => {
      abortInFlight();
      if (copyTimerRef.current !== null) {
        window.clearTimeout(copyTimerRef.current);
      }
    };
  }, [abortInFlight]);

  // 弹窗关闭或目标文件切换时立即重置全部状态，并启动首屏分页加载。
  useEffect(() => {
    abortInFlight();
    if (copyTimerRef.current !== null) {
      window.clearTimeout(copyTimerRef.current);
      copyTimerRef.current = null;
    }
    requestSeqRef.current += 1;
    const currentSeq = requestSeqRef.current;

    contentTextRef.current = "";
    setContentText("");
    setSourceKind(null);
    setNextOffset(0);
    setEof(false);
    setTruncated(false);
    setResolvedFile(null);
    setLoadingInitial(false);
    setLoadingMore(false);
    setInitialError(null);
    setLoadMoreError(null);
    setViewMode("rendered");
    setCopied(false);
    onMetaChangeRef.current?.({
      ...EMPTY_FILE_TEXT_PREVIEW_META,
      fileId,
      isExtractedDocument: previewKind === "document",
    });

    if (!open || !fileId || knownExtractionFailed) {
      return;
    }

    const controller = new AbortController();
    activeAbortRef.current = controller;
    setLoadingInitial(true);

    void getSessionFileContent(sessionId, fileId, {
      limitBytes: SESSION_FILE_CONTENT_DEFAULT_LIMIT_BYTES,
      offset: 0,
      signal: controller.signal,
    })
      .then((response) => {
        if (requestSeqRef.current !== currentSeq) {
          return;
        }
        activeAbortRef.current = null;
        contentTextRef.current = response.content;
        setContentText(response.content);
        setSourceKind(response.source_kind);
        setNextOffset(response.next_offset);
        setEof(response.eof);
        setTruncated(response.truncated);
        setResolvedFile(response.file);
        setLoadingInitial(false);
        const jsonPreview = formatJsonPreview(
          previewKind,
          response.eof,
          response.content,
        );
        onMetaChangeRef.current?.({
          eof: response.eof,
          fileId,
          hasLoadedTextContent: true,
          isExtractedDocument:
            previewKind === "document" ||
            response.source_kind === "extracted_document",
          isJsonPretty: Boolean(jsonPreview?.pretty),
          resolvedFile: response.file,
        });
        onFileMetadataUpdatedRef.current?.(response.file);
      })
      .catch((error: unknown) => {
        if (requestSeqRef.current !== currentSeq || isAbortError(error)) {
          return;
        }
        activeAbortRef.current = null;
        setInitialError(error);
        setLoadingInitial(false);
        onMetaChangeRef.current?.({
          ...EMPTY_FILE_TEXT_PREVIEW_META,
          fileId,
          isExtractedDocument: previewKind === "document",
        });
        if (isDocumentExtractionErrorCode(fileErrorCode(error))) {
          onExtractionFailedRef.current?.();
        }
      });

    return () => {
      controller.abort();
    };
  }, [
    abortInFlight,
    fileId,
    knownExtractionFailed,
    open,
    previewKind,
    reloadAttempt,
    sessionId,
  ]);

  const handleLoadMore = useCallback(async () => {
    if (
      !open ||
      !fileId ||
      eof ||
      loadingInitial ||
      loadingMoreRef.current
    ) {
      return;
    }
    loadingMoreRef.current = true;
    setLoadingMore(true);
    setLoadMoreError(null);

    const currentSeq = requestSeqRef.current;
    const controller = new AbortController();
    activeAbortRef.current = controller;

    try {
      const response = await getSessionFileContent(sessionId, fileId, {
        limitBytes: SESSION_FILE_CONTENT_DEFAULT_LIMIT_BYTES,
        offset: nextOffset,
        signal: controller.signal,
      });
      if (requestSeqRef.current !== currentSeq) {
        return;
      }
      activeAbortRef.current = null;
      // 续读必须按服务端返回的 UTF-8 安全分片依次拼接，并推进下一次读取的 next_offset。
      const nextContent = contentTextRef.current + response.content;
      contentTextRef.current = nextContent;
      setContentText(nextContent);
      setSourceKind(response.source_kind);
      setNextOffset(response.next_offset);
      setEof(response.eof);
      setTruncated(response.truncated);
      setResolvedFile(response.file);
      const jsonPreview = formatJsonPreview(
        previewKind,
        response.eof,
        nextContent,
      );
      onMetaChangeRef.current?.({
        eof: response.eof,
        fileId,
        hasLoadedTextContent: true,
        isExtractedDocument:
          previewKind === "document" ||
          response.source_kind === "extracted_document",
        isJsonPretty: Boolean(jsonPreview?.pretty),
        resolvedFile: response.file,
      });
      onFileMetadataUpdatedRef.current?.(response.file);
    } catch (error: unknown) {
      if (requestSeqRef.current !== currentSeq || isAbortError(error)) {
        return;
      }
      activeAbortRef.current = null;
      setLoadMoreError(error);
    } finally {
      if (requestSeqRef.current === currentSeq) {
        loadingMoreRef.current = false;
        setLoadingMore(false);
      }
    }
  }, [eof, fileId, loadingInitial, nextOffset, open, previewKind, sessionId]);

  const handleCopyContent = useCallback(async () => {
    if (!contentText) {
      return;
    }
    const ok = await copyTextToClipboard(contentText);
    if (!ok) {
      toast.error("当前环境无法自动复制，请手动选中复制");
      return;
    }
    setCopied(true);
    toast.success("已复制到剪贴板");
    if (copyTimerRef.current !== null) {
      window.clearTimeout(copyTimerRef.current);
    }
    copyTimerRef.current = window.setTimeout(() => {
      copyTimerRef.current = null;
      setCopied(false);
    }, COPY_FEEDBACK_MS);
  }, [contentText]);

  const effectiveExtractedTextBytes =
    resolvedFile?.extracted_text_bytes ?? file.extractedTextBytes ?? null;
  const isExtractedDocument =
    previewKind === "document" || sourceKind === "extracted_document";

  const formattedJson = useMemo(
    () => formatJsonPreview(previewKind, eof, contentText),
    [contentText, eof, previewKind],
  );

  const initialErrorCode = fileErrorCode(initialError);
  const showExtractionFailureCard =
    knownExtractionFailed ||
    Boolean(initialErrorCode && isDocumentExtractionErrorCode(initialErrorCode));
  const effectiveExtractionErrorCode = knownExtractionFailed
    ? extractionErrorCode
    : initialErrorCode;

  const hasLoadedTextContent =
    !loadingInitial && !showExtractionFailureCard && !initialError;

  const supportsViewToggle =
    hasLoadedTextContent &&
    (previewKind === "markdown" ||
      previewKind === "csv" ||
      isExtractedDocument);

  return (
    <>
      {supportsViewToggle ||
      (hasLoadedTextContent && contentText.length > 0) ? (
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border-subtle bg-subtle/60 px-6 py-2">
          <div className="flex flex-wrap items-center gap-2">
            {supportsViewToggle ? (
              <div
                aria-label="预览模式切换"
                className="inline-flex items-center gap-0.5 rounded-lg border border-border-default bg-canvas p-0.5"
                role="group"
              >
                <button
                  aria-pressed={viewMode === "rendered"}
                  className={cn(
                    "inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                    viewMode === "rendered"
                      ? "bg-brand-subtle text-brand"
                      : "text-text-secondary hover:text-text-primary",
                  )}
                  data-compact-control="true"
                  onClick={() => setViewMode("rendered")}
                  type="button"
                >
                  {previewKind === "csv" ? (
                    <Table2 aria-hidden="true" className="h-3.5 w-3.5" />
                  ) : (
                    <Eye aria-hidden="true" className="h-3.5 w-3.5" />
                  )}
                  <span>
                    {previewKind === "csv"
                      ? "表格预览"
                      : isExtractedDocument
                        ? "结构化预览"
                        : "渲染预览"}
                  </span>
                </button>
                <button
                  aria-pressed={viewMode === "raw"}
                  className={cn(
                    "inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                    viewMode === "raw"
                      ? "bg-brand-subtle text-brand"
                      : "text-text-secondary hover:text-text-primary",
                  )}
                  data-compact-control="true"
                  onClick={() => setViewMode("raw")}
                  type="button"
                >
                  <Code2 aria-hidden="true" className="h-3.5 w-3.5" />
                  <span>
                    {previewKind === "csv"
                      ? "原始文本"
                      : isExtractedDocument
                        ? "纯文本"
                        : "源码"}
                  </span>
                </button>
              </div>
            ) : null}
          </div>

          {hasLoadedTextContent && contentText.length > 0 ? (
            <Button
              aria-label={copied ? "已复制全文" : "一键复制"}
              onClick={() => void handleCopyContent()}
              size="xs"
              variant="outline"
            >
              {copied ? (
                <>
                  <Check aria-hidden="true" className="h-3.5 w-3.5 text-success" />
                  <span className="text-success">已复制</span>
                </>
              ) : (
                <>
                  <Copy aria-hidden="true" className="h-3.5 w-3.5" />
                  <span>一键复制</span>
                </>
              )}
            </Button>
          ) : null}
        </div>
      ) : null}

      <DialogBody className="space-y-4">
        {showExtractionFailureCard ? (
          <InlineAlert
            description={extractionErrorSummary(effectiveExtractionErrorCode)}
            title="暂无法在线提取文档文本"
            variant="warning"
          />
        ) : loadingInitial ? (
          <div
            aria-label="正在加载文件内容"
            className="space-y-3 py-2"
            role="status"
          >
            <span className="sr-only">正在加载文件内容…</span>
            <Skeleton className="h-5 w-1/3" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-5/6" />
            <Skeleton className="h-4 w-4/5" />
            <Skeleton className="h-24 w-full rounded-lg" />
          </div>
        ) : initialError ? (
          <InlineAlert
            action={
              <Button
                onClick={() => setReloadAttempt((attempt) => attempt + 1)}
                size="xs"
                variant="outline"
              >
                <RefreshCw aria-hidden="true" className="h-3 w-3" />
                重试
              </Button>
            }
            description={fileErrorMessage(initialError)}
            title="文件内容加载失败"
            variant="danger"
          />
        ) : (
          <>
            {/* 办公文档提取文本提示条 */}
            {isExtractedDocument ? (
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border-subtle bg-subtle/70 px-3 py-2 text-xs text-text-secondary">
                <span>
                  自动提取文本预览
                  {effectiveExtractedTextBytes !== null
                    ? ` · ${formatBytes(effectiveExtractedTextBytes)}`
                    : ""}
                </span>
                <span className="text-[11px] text-text-tertiary">
                  排版样式、图表与公式细节请以原件为准
                </span>
              </div>
            ) : null}

            {/* 空文件兜底 */}
            {contentText.length === 0 ? (
              <p className="rounded-lg border border-border-subtle bg-subtle px-4 py-6 text-center text-xs text-text-secondary">
                当前文件内容为空。
              </p>
            ) : previewKind === "csv" && viewMode === "rendered" ? (
              <FileCsvPreview contentText={contentText} />
            ) : (previewKind === "markdown" || isExtractedDocument) &&
              viewMode === "rendered" ? (
              <div className="rounded-xl border border-border-subtle bg-canvas px-4 py-3.5">
                <MarkdownContent text={contentText} />
              </div>
            ) : (
              <div className="space-y-2">
                {previewKind === "json" && !eof ? (
                  <p className="text-xs text-text-tertiary">
                    JSON 文件较长，当前按原始文本分段展示；加载全部内容后将自动格式化缩进。
                  </p>
                ) : null}
                <pre
                  className="m-0 max-w-full overflow-x-auto rounded-xl border border-border-default bg-subtle p-4 font-mono text-xs leading-relaxed break-words whitespace-pre-wrap text-text-primary"
                  data-testid="file-preview-raw-text"
                >
                  {previewKind === "json"
                    ? (formattedJson?.text ?? contentText)
                    : contentText}
                </pre>
              </div>
            )}

            {/* 分页续读错误保留已读内容，支持就地重试加载下一片。 */}
            {loadMoreError ? (
              <InlineAlert
                action={
                  <Button
                    onClick={() => void handleLoadMore()}
                    size="xs"
                    variant="outline"
                  >
                    <RefreshCw aria-hidden="true" className="h-3 w-3" />
                    重试加载更多
                  </Button>
                }
                description={fileErrorMessage(loadMoreError)}
                variant="danger"
              />
            ) : null}

            {/* 分页续读控制栏：未到 eof 时允许按 next_offset 依次续读。 */}
            {!eof || truncated ? (
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border-subtle bg-subtle/60 px-3.5 py-2.5 text-xs text-text-secondary">
                <span>
                  已加载 {formatBytes(nextOffset)}，后续仍有未读取内容
                </span>
                <Button
                  loading={loadingMore}
                  onClick={() => void handleLoadMore()}
                  size="xs"
                  variant="outline"
                >
                  {loadingMore ? "正在加载更多…" : "加载更多"}
                </Button>
              </div>
            ) : null}
          </>
        )}
      </DialogBody>
    </>
  );
}
