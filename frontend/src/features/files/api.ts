import {
  apiDownload,
  apiMultipart,
  apiRequest,
  type DownloadedFile,
} from "../../shared/api/client";
import type { SessionFile, SessionFilePage } from "./model";

function sessionFilesPath(sessionId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/files`;
}

export function listSessionFiles(
  sessionId: string,
  afterFileId: string | null = null,
  limit = 50,
): Promise<SessionFilePage> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (afterFileId) {
    params.set("after_file_id", afterFileId);
  }
  return apiRequest<SessionFilePage>(
    `${sessionFilesPath(sessionId)}?${params.toString()}`,
    { method: "GET" },
  );
}

export function uploadSessionFile(
  sessionId: string,
  file: File,
  requestId: string,
): Promise<SessionFile> {
  const form = new FormData();
  form.set("file", file);
  return apiMultipart<SessionFile>(sessionFilesPath(sessionId), form, {
    method: "POST",
    headers: { "Idempotency-Key": requestId },
  });
}

export function downloadSessionFile(
  sessionId: string,
  fileId: string,
): Promise<DownloadedFile> {
  return apiDownload(
    `${sessionFilesPath(sessionId)}/${encodeURIComponent(
      fileId,
    )}/download`,
    { method: "GET" },
  );
}
