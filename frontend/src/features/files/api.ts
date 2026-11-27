import {
  apiDownload,
  apiGet,
  apiMultipart,
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
  const query: { limit: number; after_file_id?: string } = { limit };
  if (afterFileId) {
    query.after_file_id = afterFileId;
  }
  return apiGet("/api/v1/sessions/{session_id}/files", {
    params: {
      path: { session_id: sessionId },
      query,
    },
  });
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
  const query: { offset?: number; limit_bytes?: number } = {};
  if (options.offset !== undefined) {
    query.offset = options.offset;
  }
  if (options.limitBytes !== undefined) {
    query.limit_bytes = options.limitBytes;
  }
  return apiGet("/api/v1/sessions/{session_id}/files/{file_id}/content", {
    params: {
      path: { file_id: fileId, session_id: sessionId },
      query,
    },
    signal: options.signal,
  });
}

export function downloadSessionFile(
  sessionId: string,
  fileId: string,
): Promise<DownloadedFile> {
  return apiDownload(
    `${sessionFilesPath(sessionId)}/${encodeURIComponent(fileId)}/download`,
    { method: "GET" },
  );
}
