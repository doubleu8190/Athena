import { useState } from "react";

import { Icon } from "../../shared/ui/Icons";
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
    <span className="knowledge-source-action">
      <button
        className={compact ? "text-button" : "button-quiet"}
        disabled={pending}
        onClick={() => void handleDownload()}
        type="button"
      >
        {!compact ? <Icon name="download" size={14} /> : null}
        {pending ? "正在准备下载…" : "下载来源"}
      </button>
      {error ? (
        <span className="knowledge-inline-error" role="alert">
          {error}
        </span>
      ) : null}
    </span>
  );
}
