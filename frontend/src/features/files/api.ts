import {
  apiDownload,
  apiMultipart,
  apiRequest,
  type DownloadedFile,
} from "../../shared/api/client";
import type {
  SessionFile,
  SessionFileContentResponse,
  SessionFilePage,
} from "./model";

function sessionFilesPath(sessionId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/files`;
}

export interface GetSessionFileContentOptions {
  offset?: number;
  limitBytes?: number;
  signal?: AbortSignal;
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

/**
 * 分段读取纯文本会话文件或办公文档提取缓存（GET /api/v1/sessions/{sessionId}/files/{fileId}/content）：
 * 续读时必须传入上次响应返回的 next_offset，由后端保障落在完整 UTF-8 字符边界。
 */
export function getSessionFileContent(
  sessionId: string,
  fileId: string,
  options: GetSessionFileContentOptions = {},
): Promise<SessionFileContentResponse> {
  const params = new URLSearchParams();
  if (options.offset !== undefined) {
    params.set("offset", String(options.offset));
  }
  if (options.limitBytes !== undefined) {
    params.set("limit_bytes", String(options.limitBytes));
  }
  const query = params.toString();
  return apiRequest<SessionFileContentResponse>(
    `${sessionFilesPath(sessionId)}/${encodeURIComponent(fileId)}/content${
      query ? `?${query}` : ""
    }`,
    {
      method: "GET",
      signal: options.signal,
    },
  );
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
