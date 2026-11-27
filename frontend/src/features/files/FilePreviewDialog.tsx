import {
  FileCode2,
  FileSpreadsheet,
  FileText,
  Image as ImageIcon,
} from "lucide-react";
import {
  memo,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { fileExtension } from "../../shared/lib/file";
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
} from "../../shared/ui";
import { FileImagePreview } from "./FileImagePreview";
import {
  EMPTY_FILE_TEXT_PREVIEW_META,
  FileTextPreview,
  type FileTextPreviewMeta,
} from "./FileTextPreview";
import {
  fileDisplayName,
  resolveFilePreviewKind,
  type FilePreviewKind,
  type FilePreviewTarget,
  type SessionFile,
} from "./model";
import { SessionFileDownloadButton } from "./SessionFileDownloadButton";

export {
  CSV_PREVIEW_MAX_COLUMNS,
  CSV_PREVIEW_MAX_ROWS,
  parseCsvPreview,
  type ParsedCsvPreview,
} from "./FileCsvPreview";

const PREVIEW_KIND_LABELS: Record<FilePreviewKind, string> = {
  image: "图片",
  markdown: "Markdown",
  csv: "CSV",
  json: "JSON",
  document: "办公文档",
  text: "文本",
  unsupported: "文件",
};

function previewKindBadgeLabel(
  kind: FilePreviewKind,
  filename: string,
): string {
  return fileExtension(filename)?.toUpperCase() || PREVIEW_KIND_LABELS[kind];
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
 * - 按文件类型分发至专门子组件，主弹窗仅保留容器、头部元信息、页脚下载/关闭控制与焦点归还；
 * - 包裹 React.memo 隔离流式消息更新引发的无关重渲染。
 */
export const FilePreviewDialog = memo(function FilePreviewDialog({
  file,
  onExtractionFailed,
  onFileMetadataUpdated,
  onOpenChange,
  open,
  sessionId,
}: FilePreviewDialogProps) {
  const [textMeta, setTextMeta] = useState<FileTextPreviewMeta>(
    EMPTY_FILE_TEXT_PREVIEW_META,
  );
  const wasOpenRef = useRef(false);
  const returnFocusElementRef = useRef<HTMLElement | null>(null);

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

  const filename = file?.filename ?? "";
  const mimeType = file?.mimeType ?? null;
  const previewKind = useMemo<FilePreviewKind>(
    () => (file ? resolveFilePreviewKind(filename, mimeType) : "unsupported"),
    [file, filename, mimeType],
  );

  const displayName = file
    ? fileDisplayName({ filename: file.filename, label: file.label ?? null })
    : "";

  // 仅消费与当前文件匹配的文本子组件元信息，避免切文件瞬间残留旧文件的徽章或页脚状态。
  const currentTextMeta =
    file &&
    previewKind !== "image" &&
    previewKind !== "unsupported" &&
    textMeta.fileId === file.fileId
      ? textMeta
      : EMPTY_FILE_TEXT_PREVIEW_META;

  const effectiveSizeBytes =
    currentTextMeta.resolvedFile?.size_bytes ?? file?.sizeBytes ?? null;
  const effectiveExtractedTextBytes =
    currentTextMeta.resolvedFile?.extracted_text_bytes ??
    file?.extractedTextBytes ??
    null;

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
                  {currentTextMeta.isExtractedDocument &&
                  currentTextMeta.hasLoadedTextContent ? (
                    <Badge className="px-1.5 py-0 text-[11px]" variant="success">
                      自动提取文本预览
                      {effectiveExtractedTextBytes !== null
                        ? ` · ${formatBytes(effectiveExtractedTextBytes)}`
                        : ""}
                    </Badge>
                  ) : null}
                  {previewKind === "json" && currentTextMeta.isJsonPretty ? (
                    <Badge className="px-1.5 py-0 text-[11px]" variant="info">
                      已格式化
                    </Badge>
                  ) : null}
                </div>
              </DialogDescription>
            </DialogHeader>

            {previewKind === "image" ? (
              <FileImagePreview
                displayName={displayName}
                fileId={file.fileId}
                sessionId={sessionId}
              />
            ) : previewKind === "unsupported" ? (
              <DialogBody className="space-y-4">
                <InlineAlert
                  description="该文件格式暂不支持在线预览，请点击下方「下载原件」在本地应用打开查看。"
                  title="暂不支持在线预览"
                  variant="info"
                />
              </DialogBody>
            ) : (
              <FileTextPreview
                file={file}
                onExtractionFailed={onExtractionFailed}
                onFileMetadataUpdated={onFileMetadataUpdated}
                onMetaChange={setTextMeta}
                open={open}
                previewKind={previewKind}
                sessionId={sessionId}
              />
            )}

            <DialogFooter className="justify-between">
              <div className="min-w-0 text-xs text-text-tertiary">
                {currentTextMeta.hasLoadedTextContent ? (
                  currentTextMeta.eof ? (
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
});
