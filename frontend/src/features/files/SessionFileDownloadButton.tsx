import { useState } from "react";

import { saveDownloadedBlob } from "../knowledge/KnowledgeSourceButton";
import { buttonVariants, TEXT_BUTTON_CLASS } from "../../shared/ui";
import { Icon } from "../../shared/ui/Icons";
import { downloadSessionFile } from "./api";
import { fileErrorMessage } from "./errorMessages";

interface SessionFileDownloadButtonProps {
  compact?: boolean;
  fileId: string;
  filename: string;
  label?: string;
  sessionId: string;
}

export function SessionFileDownloadButton({
  compact = false,
  fileId,
  filename,
  label = "下载",
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
    <span className="inline-flex min-w-0 flex-col items-end gap-[3px]">
      <button
        aria-label={`下载文件：${filename}`}
        className={
          compact
            ? TEXT_BUTTON_CLASS
            : buttonVariants({ size: "sm", variant: "outline" })
        }
        disabled={pending}
        onClick={() => void handleDownload()}
        type="button"
      >
        {!compact ? <Icon name="download" size={14} /> : null}
        {pending ? "正在准备…" : label}
      </button>
      {error ? (
        <span className="text-xs leading-normal text-danger" role="alert">
          {error}
        </span>
      ) : null}
    </span>
  );
}
