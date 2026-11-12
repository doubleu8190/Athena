import { useState } from "react";

import { saveDownloadedBlob } from "../knowledge/KnowledgeSourceButton";
import { Icon } from "../../shared/ui/Icons";
import { downloadSessionFile } from "./api";
import { fileErrorMessage } from "./errorMessages";

interface SessionFileDownloadButtonProps {
  compact?: boolean;
  fileId: string;
  filename: string;
  sessionId: string;
}

export function SessionFileDownloadButton({
  compact = false,
  fileId,
  filename,
  sessionId,
}: SessionFileDownloadButtonProps) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDownload = async () => {
    if (pending) {
      return;
    }
    setPending(true);
    setError(null);
    try {
      const file = await downloadSessionFile(sessionId, fileId);
      saveDownloadedBlob(file.blob, file.filename || filename);
    } catch (failure) {
      setError(fileErrorMessage(failure));
    } finally {
      setPending(false);
    }
  };

  return (
    <span className="session-file-download">
      <button
        aria-label={`下载文件：${filename}`}
        className={compact ? "text-button" : "button-quiet"}
        disabled={pending}
        onClick={() => void handleDownload()}
        type="button"
      >
        {!compact ? <Icon name="download" size={14} /> : null}
        {pending ? "正在准备…" : "下载"}
      </button>
      {error ? (
        <span className="session-file-inline-error" role="alert">
          {error}
        </span>
      ) : null}
    </span>
  );
}
