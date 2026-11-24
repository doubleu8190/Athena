import { FileText, Image, Paperclip, RefreshCw, Sparkles } from "lucide-react";
import { useMemo } from "react";

import { formatBytes, formatDateTime } from "../../shared/lib/format";
import {
  Badge,
  Button,
  EmptyState,
  InlineAlert,
  Sheet,
  SheetBody,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  Skeleton,
} from "../../shared/ui";
import { fileErrorMessage } from "../files/errorMessages";
import { useSessionFiles } from "../files/hooks";
import { fileDisplayName, type SessionFile } from "../files/model";
import { SessionFileDownloadButton } from "../files/SessionFileDownloadButton";
import { isImageAttachmentFile } from "./ComposerAttachmentBar";

export interface SessionFilesDrawerProps {
  onOpenChange: (open: boolean) => void;
  open: boolean;
  sessionId: string;
}

function FileRow({
  file,
  sessionId,
}: {
  file: SessionFile;
  sessionId: string;
}) {
  const displayName = fileDisplayName(file);
  const isImage = isImageAttachmentFile(file.filename, file.mime_type);
  const FileIcon = isImage ? Image : FileText;

  return (
    <li className="flex items-start justify-between gap-3 rounded-xl border border-border-subtle bg-canvas p-3 transition-colors hover:border-border-default">
      <div className="flex min-w-0 flex-1 items-start gap-2.5">
        <span
          aria-hidden="true"
          className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-subtle text-text-secondary"
        >
          <FileIcon className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1 space-y-0.5">
          <p className="truncate text-sm font-medium text-text-primary">
            {displayName}
          </p>
          {displayName !== file.filename ? (
            <p className="truncate text-xs text-text-tertiary">
              {file.filename}
            </p>
          ) : null}
          <p className="text-[11px] text-text-tertiary">
            {formatBytes(file.size_bytes)} · {formatDateTime(file.created_at)}
          </p>
        </div>
      </div>

      <div className="shrink-0 self-center">
        <SessionFileDownloadButton
          compact
          fileId={file.id}
          filename={displayName}
          sessionId={sessionId}
        />
      </div>
    </li>
  );
}

/**
 * 右侧会话文件抽屉（Radix Sheet）：
 * 集中展示本会话的全部「上传附件」与「生成产物」，提供文件元信息与一键下载。
 */
export function SessionFilesDrawer({
  onOpenChange,
  open,
  sessionId,
}: SessionFilesDrawerProps) {
  const filesQuery = useSessionFiles(sessionId, Boolean(sessionId));

  const uniqueFiles = useMemo(() => {
    const flattened = (filesQuery.data?.pages ?? []).flatMap(
      (page) => page.items,
    );
    return Array.from(
      new Map(flattened.map((file) => [file.id, file])).values(),
    );
  }, [filesQuery.data?.pages]);

  const uploadedFiles = useMemo(
    () => uniqueFiles.filter((file) => file.source === "upload"),
    [uniqueFiles],
  );
  const artifactFiles = useMemo(
    () => uniqueFiles.filter((file) => file.source === "artifact"),
    [uniqueFiles],
  );

  return (
    <Sheet onOpenChange={onOpenChange} open={open}>
      <SheetContent side="right">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            <span>会话文件</span>
            {uniqueFiles.length > 0 ? (
              <Badge variant="neutral">{uniqueFiles.length}</Badge>
            ) : null}
          </SheetTitle>
          <SheetDescription>
            集中查看并下载本对话上传的附件与助手生成的交付文件。
          </SheetDescription>
        </SheetHeader>

        <SheetBody className="space-y-5">
          {filesQuery.isPending ? (
            <div aria-label="正在加载会话文件" className="space-y-3" role="status">
              <Skeleton className="h-16 w-full rounded-xl" />
              <Skeleton className="h-16 w-full rounded-xl" />
              <Skeleton className="h-16 w-full rounded-xl" />
            </div>
          ) : filesQuery.isError ? (
            <InlineAlert
              action={
                <Button
                  onClick={() => void filesQuery.refetch()}
                  size="xs"
                  variant="outline"
                >
                  <RefreshCw aria-hidden="true" className="h-3 w-3" />
                  重试
                </Button>
              }
              description={fileErrorMessage(filesQuery.error)}
              title="会话文件加载失败"
              variant="danger"
            />
          ) : uniqueFiles.length === 0 ? (
            <EmptyState
              compact
              description="上传的参考资料和助手生成的报表、文档都会自动归档到这里。"
              icon="folder"
              title="暂无会话文件"
            />
          ) : (
            <>
              {artifactFiles.length > 0 ? (
                <section aria-label="生成产物" className="space-y-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="flex items-center gap-1.5 text-xs font-semibold text-text-secondary">
                      <Sparkles
                        aria-hidden="true"
                        className="h-3.5 w-3.5 text-brand"
                      />
                      <span>生成产物</span>
                    </h3>
                    <span className="text-xs text-text-tertiary">
                      {artifactFiles.length} 个
                    </span>
                  </div>
                  <ul className="space-y-2">
                    {artifactFiles.map((file) => (
                      <FileRow
                        file={file}
                        key={file.id}
                        sessionId={sessionId}
                      />
                    ))}
                  </ul>
                </section>
              ) : null}

              {uploadedFiles.length > 0 ? (
                <section aria-label="上传附件" className="space-y-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="flex items-center gap-1.5 text-xs font-semibold text-text-secondary">
                      <Paperclip
                        aria-hidden="true"
                        className="h-3.5 w-3.5 text-text-tertiary"
                      />
                      <span>上传附件</span>
                    </h3>
                    <span className="text-xs text-text-tertiary">
                      {uploadedFiles.length} 个
                    </span>
                  </div>
                  <ul className="space-y-2">
                    {uploadedFiles.map((file) => (
                      <FileRow
                        file={file}
                        key={file.id}
                        sessionId={sessionId}
                      />
                    ))}
                  </ul>
                </section>
              ) : null}

              {filesQuery.hasNextPage ? (
                <Button
                  className="w-full"
                  disabled={filesQuery.isFetchingNextPage}
                  onClick={() => void filesQuery.fetchNextPage()}
                  size="sm"
                  variant="outline"
                >
                  {filesQuery.isFetchingNextPage
                    ? "正在加载更多…"
                    : "加载更多文件"}
                </Button>
              ) : null}
            </>
          )}
        </SheetBody>
      </SheetContent>
    </Sheet>
  );
}
