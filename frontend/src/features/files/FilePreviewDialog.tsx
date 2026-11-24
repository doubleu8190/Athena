import {
  Check,
  Code2,
  Copy,
  Eye,
  FileCode2,
  FileSpreadsheet,
  FileText,
  Image as ImageIcon,
  RefreshCw,
  Table2,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { copyTextToClipboard } from "../../shared/lib/clipboard";
import { formatBytes } from "../../shared/lib/format";
import {
  Badge,
  Button,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
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
import {
  SESSION_FILE_CONTENT_DEFAULT_LIMIT_BYTES,
  fileDisplayName,
  fileExtension,
  resolveFilePreviewKind,
  sessionFilePreviewUrl,
  type FilePreviewKind,
  type FilePreviewTarget,
  type SessionFile,
  type SessionFileContentSourceKind,
} from "./model";
import { SessionFileDownloadButton } from "./SessionFileDownloadButton";

const COPY_FEEDBACK_MS = 2_000;

/** CSV 在线表格渲染硬上限（最多 200 行数据 × 50 列），避免大表导致主线程卡顿。 */
export const CSV_PREVIEW_MAX_ROWS = 200;
export const CSV_PREVIEW_MAX_COLUMNS = 50;

export interface ParsedCsvPreview {
  headers: string[];
  rows: string[][];
  columnCount: number;
  rowsTruncated: boolean;
  columnsTruncated: boolean;
}

/**
 * 零依赖 RFC 4180 安全 CSV 解析器：
 * - 正确处理双引号转义（"" -> "）、引号内逗号与引号内换行（\r\n / \n）；
 * - 严格按上限（默认 200 行数据 × 50 列）提前截断，防止超大表格阻塞渲染；
 * - 所有单元格均作为纯文本交给 React 转义渲染，杜绝 HTML/脚本注入。
 */
export function parseCsvPreview(
  input: string,
  options: { maxRows?: number; maxColumns?: number } = {},
): ParsedCsvPreview {
  const maxRows = options.maxRows ?? CSV_PREVIEW_MAX_ROWS;
  const maxColumns = options.maxColumns ?? CSV_PREVIEW_MAX_COLUMNS;
  // 剥离常见 UTF-8 BOM，避免首列表头出现不可见前缀。
  const text = input.startsWith("\uFEFF") ? input.slice(1) : input;
  if (!text.trim()) {
    return {
      columnCount: 0,
      columnsTruncated: false,
      headers: [],
      rows: [],
      rowsTruncated: false,
    };
  }

  const allRows: string[][] = [];
  let currentRow: string[] = [];
  let currentField = "";
  let inQuotes = false;
  let fieldStarted = false;
  let rowsTruncated = false;
  let columnsTruncated = false;

  // 允许收集的总行数 = 1 行表头 + maxRows 行数据。
  const maxCollectedRows = maxRows + 1;

  const pushField = () => {
    if (currentRow.length < maxColumns) {
      currentRow.push(currentField);
    } else {
      columnsTruncated = true;
    }
    currentField = "";
    fieldStarted = false;
  };

  const pushRow = (): boolean => {
    if (allRows.length >= maxCollectedRows) {
      rowsTruncated = true;
      return false;
    }
    allRows.push(currentRow);
    currentRow = [];
    return true;
  };

  for (let index = 0; index < text.length; index += 1) {
    const ch = text[index];

    if (inQuotes) {
      if (ch === '"') {
        if (index + 1 < text.length && text[index + 1] === '"') {
          currentField += '"';
          index += 1;
        } else {
          inQuotes = false;
        }
      } else if (ch === "\r") {
        if (index + 1 < text.length && text[index + 1] === "\n") {
          index += 1;
        }
        currentField += "\n";
      } else {
        currentField += ch;
      }
      continue;
    }

    if (ch === '"' && !fieldStarted && currentField.length === 0) {
      inQuotes = true;
      fieldStarted = true;
      continue;
    }

    if (ch === ",") {
      pushField();
      continue;
    }

    if (ch === "\r" || ch === "\n") {
      if (ch === "\r" && index + 1 < text.length && text[index + 1] === "\n") {
        index += 1;
      }
      pushField();
      if (!pushRow()) {
        break;
      }
      continue;
    }

    currentField += ch;
    fieldStarted = true;
  }

  // 收尾最后一行（若文件以换行结尾则不生成空尾行）。
  if (
    !rowsTruncated &&
    (fieldStarted || currentField.length > 0 || currentRow.length > 0)
  ) {
    pushField();
    pushRow();
  }

  if (allRows.length === 0) {
    return {
      columnCount: 0,
      columnsTruncated: false,
      headers: [],
      rows: [],
      rowsTruncated: false,
    };
  }

  const columnCount = Math.min(
    maxColumns,
    allRows.reduce((max, row) => Math.max(max, row.length), 0),
  );
  const padRow = (row: string[]): string[] =>
    row.length >= columnCount
      ? row.slice(0, columnCount)
      : [...row, ...Array<string>(columnCount - row.length).fill("")];

  const [rawHeaders, ...rawDataRows] = allRows;
  return {
    columnCount,
    columnsTruncated,
    headers: padRow(rawHeaders),
    rows: rawDataRows.map(padRow),
    rowsTruncated,
  };
}

function previewKindBadgeLabel(
  kind: FilePreviewKind,
  filename: string,
): string {
  const ext = fileExtension(filename);
  if (ext) {
    return ext.toUpperCase();
  }
  switch (kind) {
    case "image":
      return "图片";
    case "markdown":
      return "Markdown";
    case "csv":
      return "CSV";
    case "json":
      return "JSON";
    case "document":
      return "办公文档";
    case "text":
      return "文本";
    default:
      return "文件";
  }
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

export interface FilePreviewDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sessionId: string;
  file: FilePreviewTarget | null;
  /** 文本读取触发后端惰性补解析成功时，回传最新 SessionFile 元数据供外层同步缓存。 */
  onFileMetadataUpdated?: (file: SessionFile) => void;
  /** 惰性补解析返回 422 提取错误码时通知外层刷新会话文件列表，使抽屉/输入器微标同步为「未提取」。 */
  onExtractionFailed?: () => void;
}

/**
 * 统一文件在线预览与原图灯箱弹窗（Radix Dialog）：
 * - 统一承载白名单位图灯箱（PNG/JPEG/WEBP）、Markdown、CSV 表格、JSON/纯文本与 PDF/Office 提取文本；
 * - 严格按后端返回的 next_offset 续读分页（直至 eof === true），并在切文件/关弹窗时通过 AbortController 彻底中止在途请求防止串染；
 * - 提取失败或格式不支持时展示温和中文原因并保留「下载原件」入口。
 */
export function FilePreviewDialog({
  file,
  onExtractionFailed,
  onFileMetadataUpdated,
  onOpenChange,
  open,
  sessionId,
}: FilePreviewDialogProps) {
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
  const [imageZoomActual, setImageZoomActual] = useState(false);
  const [imageLoaded, setImageLoaded] = useState(false);
  const [imageFailed, setImageFailed] = useState(false);
  const [imageReloadCount, setImageReloadCount] = useState(0);
  const [copied, setCopied] = useState(false);
  const [reloadAttempt, setReloadAttempt] = useState(0);

  // 递增请求序号与在途 AbortController 共同保证切文件或关弹窗时零串染。
  const requestSeqRef = useRef(0);
  const activeAbortRef = useRef<AbortController | null>(null);
  const loadingMoreRef = useRef(false);
  const copyTimerRef = useRef<number | null>(null);
  const wasOpenRef = useRef(false);
  const returnFocusElementRef = useRef<HTMLElement | null>(null);
  const onFileMetadataUpdatedRef = useRef(onFileMetadataUpdated);
  onFileMetadataUpdatedRef.current = onFileMetadataUpdated;
  const onExtractionFailedRef = useRef(onExtractionFailed);
  onExtractionFailedRef.current = onExtractionFailed;

  const isDialogOpen = open && Boolean(file);

  // 受控弹窗未包裹 DialogTrigger；在打开切换的 layout 阶段（早于 Radix FocusScope 的 useEffect 夺焦）
  // 捕获当前焦点触发器，确保弹窗关闭（包括从 SessionFilesDrawer 叠层关闭）时焦点准确归还而不跌落 document.body。
  useLayoutEffect(() => {
    if (isDialogOpen && !wasOpenRef.current) {
      const active =
        typeof document !== "undefined" ? document.activeElement : null;
      returnFocusElementRef.current =
        active instanceof HTMLElement && active !== document.body
          ? active
          : null;
    }
    wasOpenRef.current = isDialogOpen;
  }, [isDialogOpen]);

  const abortInFlight = useCallback(() => {
    if (activeAbortRef.current) {
      activeAbortRef.current.abort();
      activeAbortRef.current = null;
    }
    loadingMoreRef.current = false;
  }, []);

  const fileId = file?.fileId ?? null;
  const filename = file?.filename ?? "";
  const mimeType = file?.mimeType ?? null;
  const extractionStatus = file?.extractionStatus;
  const extractionErrorCode = file?.extractionErrorCode ?? null;

  const previewKind = useMemo<FilePreviewKind>(
    () => (file ? resolveFilePreviewKind(filename, mimeType) : "unsupported"),
    [file, filename, mimeType],
  );

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

  // 弹窗关闭或目标文件切换时立即重置全部状态，并按预览形态启动首屏加载。
  useEffect(() => {
    abortInFlight();
    if (copyTimerRef.current !== null) {
      window.clearTimeout(copyTimerRef.current);
      copyTimerRef.current = null;
    }
    requestSeqRef.current += 1;
    const currentSeq = requestSeqRef.current;

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
    setImageZoomActual(false);
    setImageLoaded(false);
    setImageFailed(false);
    setImageReloadCount(0);
    setCopied(false);

    if (!open || !fileId) {
      return;
    }

    if (
      previewKind === "image" ||
      previewKind === "unsupported" ||
      knownExtractionFailed
    ) {
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
        setContentText(response.content);
        setSourceKind(response.source_kind);
        setNextOffset(response.next_offset);
        setEof(response.eof);
        setTruncated(response.truncated);
        setResolvedFile(response.file);
        setLoadingInitial(false);
        onFileMetadataUpdatedRef.current?.(response.file);
      })
      .catch((error: unknown) => {
        if (requestSeqRef.current !== currentSeq || isAbortError(error)) {
          return;
        }
        activeAbortRef.current = null;
        setInitialError(error);
        setLoadingInitial(false);
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
      setContentText((previous) => previous + response.content);
      setSourceKind(response.source_kind);
      setNextOffset(response.next_offset);
      setEof(response.eof);
      setTruncated(response.truncated);
      setResolvedFile(response.file);
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
  }, [eof, fileId, loadingInitial, nextOffset, open, sessionId]);

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

  const displayName = file
    ? fileDisplayName({ filename: file.filename, label: file.label ?? null })
    : "";
  const effectiveSizeBytes = resolvedFile?.size_bytes ?? file?.sizeBytes ?? null;
  const effectiveExtractedTextBytes =
    resolvedFile?.extracted_text_bytes ?? file?.extractedTextBytes ?? null;
  const isExtractedDocument =
    previewKind === "document" || sourceKind === "extracted_document";

  const parsedCsv = useMemo(() => {
    if (previewKind !== "csv" || !contentText) {
      return null;
    }
    return parseCsvPreview(contentText);
  }, [contentText, previewKind]);

  // JSON 仅在完整加载（eof === true）且语法合法时自动格式化，避免截断分片强行解析报错。
  const formattedJson = useMemo<{ text: string; pretty: boolean } | null>(() => {
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
  }, [contentText, eof, previewKind]);

  const initialErrorCode = fileErrorCode(initialError);
  const showExtractionFailureCard =
    knownExtractionFailed ||
    Boolean(initialErrorCode && isDocumentExtractionErrorCode(initialErrorCode));
  const effectiveExtractionErrorCode = knownExtractionFailed
    ? extractionErrorCode
    : initialErrorCode;

  const hasLoadedTextContent =
    previewKind !== "image" &&
    previewKind !== "unsupported" &&
    !loadingInitial &&
    !showExtractionFailureCard &&
    !initialError;

  const supportsViewToggle =
    hasLoadedTextContent &&
    (previewKind === "markdown" ||
      previewKind === "csv" ||
      isExtractedDocument);

  const imagePreviewUrl =
    file && previewKind === "image"
      ? `${sessionFilePreviewUrl(sessionId, file.fileId)}${
          imageReloadCount > 0 ? `?retry=${imageReloadCount}` : ""
        }`
      : null;

  const HeaderIcon =
    previewKind === "image"
      ? ImageIcon
      : previewKind === "csv"
        ? FileSpreadsheet
        : previewKind === "json" || previewKind === "markdown"
          ? FileCode2
          : FileText;

  return (
    <Dialog onOpenChange={onOpenChange} open={isDialogOpen}>
      <DialogContent
        className="max-w-4xl"
        data-testid="file-preview-dialog"
        onCloseAutoFocus={(event) => {
          // 接管受控弹窗关闭后的焦点归还，将焦点精准送回仍挂载在文档中的原触发按钮。
          const target = returnFocusElementRef.current;
          returnFocusElementRef.current = null;
          if (target && target.isConnected) {
            event.preventDefault();
            target.focus();
          }
        }}
        size="lg"
      >
        {file ? (
          <>
            <DialogHeader>
              <DialogTitle className="flex min-w-0 items-center gap-2 pr-2">
                <HeaderIcon
                  aria-hidden="true"
                  className="h-4 w-4 shrink-0 text-brand"
                />
                <span className="min-w-0 truncate" title={displayName}>
                  {displayName}
                </span>
              </DialogTitle>
              <DialogDescription asChild>
                <div className="flex flex-wrap items-center gap-2 text-xs text-text-secondary">
                  <Badge className="px-1.5 py-0 text-[11px]" variant="neutral">
                    {previewKindBadgeLabel(previewKind, file.filename)}
                  </Badge>
                  {effectiveSizeBytes !== null ? (
                    <span className="tabular-nums">
                      {formatBytes(effectiveSizeBytes)}
                    </span>
                  ) : null}
                  {displayName !== file.filename ? (
                    <span
                      className="max-w-full truncate text-text-tertiary"
                      title={file.filename}
                    >
                      {file.filename}
                    </span>
                  ) : null}
                  {isExtractedDocument && hasLoadedTextContent ? (
                    <Badge className="px-1.5 py-0 text-[11px]" variant="success">
                      自动提取文本预览
                      {effectiveExtractedTextBytes !== null
                        ? ` · ${formatBytes(effectiveExtractedTextBytes)}`
                        : ""}
                    </Badge>
                  ) : null}
                  {previewKind === "json" && formattedJson?.pretty ? (
                    <Badge className="px-1.5 py-0 text-[11px]" variant="info">
                      已格式化
                    </Badge>
                  ) : null}
                </div>
              </DialogDescription>
            </DialogHeader>

            {/* 预览工具条：图片缩放开关、Markdown/CSV/文档视图切换与一键复制。 */}
            {(previewKind === "image" && !imageFailed) ||
            supportsViewToggle ||
            (hasLoadedTextContent && contentText.length > 0) ? (
              <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border-subtle bg-subtle/60 px-6 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  {previewKind === "image" && !imageFailed ? (
                    <>
                      <Button
                        onClick={() => setImageZoomActual((current) => !current)}
                        size="xs"
                        variant="outline"
                      >
                        {imageZoomActual ? (
                          <>
                            <ZoomOut aria-hidden="true" className="h-3.5 w-3.5" />
                            <span>适应窗口</span>
                          </>
                        ) : (
                          <>
                            <ZoomIn aria-hidden="true" className="h-3.5 w-3.5" />
                            <span>100% 原始尺寸</span>
                          </>
                        )}
                      </Button>
                      <span className="text-xs text-text-tertiary">
                        点击图片可切换缩放比例
                      </span>
                    </>
                  ) : null}

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
                        <Check
                          aria-hidden="true"
                          className="h-3.5 w-3.5 text-success"
                        />
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
              {/* 形态 1：白名单位图原图灯箱（PNG / JPEG / WEBP） */}
              {previewKind === "image" && imagePreviewUrl ? (
                imageFailed ? (
                  <InlineAlert
                    action={
                      <Button
                        onClick={() => {
                          setImageFailed(false);
                          setImageLoaded(false);
                          setImageReloadCount((count) => count + 1);
                        }}
                        size="xs"
                        variant="outline"
                      >
                        <RefreshCw aria-hidden="true" className="h-3 w-3" />
                        重新加载
                      </Button>
                    }
                    description="图片可能已过期或文件服务暂时不可用，请重试或点击下方按钮下载原件。"
                    title="原图预览加载失败"
                    variant="warning"
                  />
                ) : (
                  <div
                    className={cn(
                      "relative flex min-h-[260px] w-full overflow-auto rounded-xl border border-border-subtle bg-subtle p-3",
                      imageZoomActual
                        ? "max-h-[64vh] items-start justify-start"
                        : "max-h-[64vh] items-center justify-center",
                    )}
                    data-image-zoom={imageZoomActual ? "actual" : "fit"}
                  >
                    {!imageLoaded ? (
                      <div
                        aria-label="正在加载原图"
                        className="pointer-events-none absolute inset-3 flex items-center justify-center"
                        role="status"
                      >
                        <Skeleton className="h-48 w-full max-w-md rounded-lg" />
                      </div>
                    ) : null}
                    <button
                      aria-label={
                        imageZoomActual
                          ? `缩放至适应窗口：${displayName}`
                          : `切换至 100% 原始尺寸：${displayName}`
                      }
                      className={cn(
                        "relative rounded-lg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                        imageZoomActual
                          ? "shrink-0 cursor-zoom-out"
                          : "min-w-0 max-w-full cursor-zoom-in",
                      )}
                      onClick={() => setImageZoomActual((current) => !current)}
                      type="button"
                    >
                      <img
                        alt={displayName}
                        className={cn(
                          "block rounded-lg transition-opacity duration-150",
                          imageZoomActual
                            ? "h-auto max-w-none w-auto"
                            : "max-h-[58vh] max-w-full object-contain",
                        )}
                        onError={() => {
                          setImageFailed(true);
                          setImageLoaded(false);
                        }}
                        onLoad={() => {
                          setImageLoaded(true);
                          setImageFailed(false);
                        }}
                        src={imagePreviewUrl}
                      />
                    </button>
                  </div>
                )
              ) : previewKind === "unsupported" ? (
                <InlineAlert
                  description="该文件格式暂不支持在线预览，请点击下方「下载原件」在本地应用打开查看。"
                  title="暂不支持在线预览"
                  variant="info"
                />
              ) : showExtractionFailureCard ? (
                <InlineAlert
                  description={extractionErrorSummary(
                    effectiveExtractionErrorCode,
                  )}
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
                  {/* 形态 5：办公文档提取文本提示条 */}
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
                    /* 形态 3：CSV 安全表格预览 */
                    <div className="space-y-2.5">
                      {parsedCsv && parsedCsv.columnCount > 0 ? (
                        <>
                          {parsedCsv.rowsTruncated ||
                          parsedCsv.columnsTruncated ? (
                            <InlineAlert
                              description={`表格规模较大，在线预览仅展示前 ${CSV_PREVIEW_MAX_ROWS} 行数据 × ${CSV_PREVIEW_MAX_COLUMNS} 列以保持流畅，查看完整数据请下载原件。`}
                              variant="info"
                            />
                          ) : null}
                          <div className="w-full overflow-x-auto rounded-lg border border-border-default bg-canvas">
                            <table
                              className="w-full border-collapse text-left text-xs"
                              data-testid="csv-preview-table"
                            >
                              <thead className="bg-subtle text-text-secondary">
                                <tr>
                                  {parsedCsv.headers.map((header, colIndex) => (
                                    <th
                                      className="border-b border-r border-border-default px-3 py-2 font-semibold whitespace-nowrap last:border-r-0"
                                      key={`csv-head-${colIndex}`}
                                      scope="col"
                                    >
                                      {header || `列 ${colIndex + 1}`}
                                    </th>
                                  ))}
                                </tr>
                              </thead>
                              <tbody>
                                {parsedCsv.rows.map((row, rowIndex) => (
                                  <tr
                                    className="border-b border-border-subtle transition-colors last:border-b-0 hover:bg-subtle/50"
                                    key={`csv-row-${rowIndex}`}
                                  >
                                    {row.map((cell, colIndex) => (
                                      <td
                                        className="max-w-[280px] border-r border-border-subtle px-3 py-2 align-top break-words whitespace-pre-wrap text-text-primary last:border-r-0"
                                        key={`csv-cell-${rowIndex}-${colIndex}`}
                                      >
                                        {cell}
                                      </td>
                                    ))}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        </>
                      ) : (
                        <p className="rounded-lg border border-border-subtle bg-subtle px-4 py-6 text-center text-xs text-text-secondary">
                          未解析到可展示的表格行，可切换「原始文本」查看。
                        </p>
                      )}
                    </div>
                  ) : (previewKind === "markdown" || isExtractedDocument) &&
                    viewMode === "rendered" ? (
                    /* 形态 2 & 5：Markdown 文档与结构化提取文档安全渲染 */
                    <div className="rounded-xl border border-border-subtle bg-canvas px-4 py-3.5">
                      <MarkdownContent text={contentText} />
                    </div>
                  ) : (
                    /* 形态 4：JSON（完整时自动格式化）与纯文本 / 源码只读等宽区 */
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

            <DialogFooter className="justify-between">
              <div className="min-w-0 text-xs text-text-tertiary">
                {hasLoadedTextContent ? (
                  eof ? (
                    <span>已加载全部内容</span>
                  ) : (
                    <span>分段预览中 · 可点击「加载更多」继续阅读</span>
                  )
                ) : previewKind === "image" ? (
                  <span>安全同源预览</span>
                ) : null}
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <SessionFileDownloadButton
                  fileId={file.fileId}
                  filename={displayName}
                  key={file.fileId}
                  label="下载原件"
                  sessionId={sessionId}
                />
                <Button
                  onClick={() => onOpenChange(false)}
                  size="sm"
                  variant="outline"
                >
                  关闭预览
                </Button>
              </div>
            </DialogFooter>
          </>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
