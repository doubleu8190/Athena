import { Download } from "lucide-react";
import { useState } from "react";

import { buttonVariants, TEXT_BUTTON_CLASS } from "../../shared/ui";
import { downloadKnowledgeSource } from "./api";
import { knowledgeErrorMessage } from "./errorMessages";

export function saveDownloadedBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename || "download";
  anchor.rel = "noopener";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  // 给浏览器留出启动下载的时间，避免过早回收 Blob URL 导致下载中断。
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

interface KnowledgeSourceButtonProps {
  baseId: string;
  compact?: boolean;
  documentId: string;
  versionId: string;
}

export function KnowledgeSourceButton({
  baseId,
  compact = false,
  documentId,
  versionId,
}: KnowledgeSourceButtonProps) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDownload = async () => {
    setPending(true);
    setError(null);
    try {
      const file = await downloadKnowledgeSource(
        baseId,
        documentId,
        versionId,
      );
      saveDownloadedBlob(file.blob, file.filename);
    } catch (failure) {
      setError(knowledgeErrorMessage(failure));
    } finally {
      setPending(false);
    }
  };

  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <button
        className={
          compact
            ? TEXT_BUTTON_CLASS
            : buttonVariants({ size: "sm", variant: "outline" })
        }
        disabled={pending}
        onClick={() => void handleDownload()}
        type="button"
      >
        {!compact ? (
          <Download
            aria-hidden="true"
            focusable="false"
            size={14}
            strokeWidth={1.7}
          />
        ) : null}
        {pending ? "正在准备下载…" : "下载来源"}
      </button>
      {error ? (
        <span
          className="block text-xs leading-[1.5] text-danger"
          role="alert"
        >
          {error}
        </span>
      ) : null}
    </span>
  );
}
