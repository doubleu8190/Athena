import {
  apiDownload,
  apiGet,
  apiMultipart,
  type ApiSchemas,
  type DownloadedFile,
} from "../../shared/api/client";
import type {
  SessionFile,
  SessionFileContentResponse,
  SessionFilePage,
  SessionFileSource,
} from "./model";

function sessionFilesPath(sessionId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/files`;
}

function toSessionFileSource(value: string): SessionFileSource {
  return value === "artifact" ? "artifact" : "upload";
}

function toSessionFile(raw: ApiSchemas["SessionFileResponse"]): SessionFile {
  return {
    ...raw,
    source: toSessionFileSource(raw.source),
  };
}

export interface GetSessionFileContentOptions {
  offset?: number;
  limitBytes?: number;
  signal?: AbortSignal;
}

export async function listSessionFiles(
  sessionId: string,
  afterFileId: string | null = null,
  limit = 50,
): Promise<SessionFilePage> {
  const query: { limit: number; after_file_id?: string } = { limit };
  if (afterFileId) {
    query.after_file_id = afterFileId;
  }
  const response = await apiGet("/api/v1/sessions/{session_id}/files", {
    params: {
      path: { session_id: sessionId },
      query,
    },
  });
  return {
    items: response.items.map(toSessionFile),
    next_after_file_id: response.next_after_file_id,
  };
}

export async function uploadSessionFile(
  sessionId: string,
  file: File,
  requestId: string,
): Promise<SessionFile> {
  const form = new FormData();
  form.set("file", file);
  const response = await apiMultipart<ApiSchemas["SessionFileResponse"]>(
    sessionFilesPath(sessionId),
    form,
    {
      method: "POST",
      headers: { "Idempotency-Key": requestId },
    },
  );
  return toSessionFile(response);
}

/**
 * 分段读取纯文本会话文件或办公文档提取缓存（GET /api/v1/sessions/{sessionId}/files/{fileId}/content）：
 * 续读时必须传入上次响应返回的 next_offset，由后端保障落在完整 UTF-8 字符边界。
 */
export async function getSessionFileContent(
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
  const response = await apiGet(
    "/api/v1/sessions/{session_id}/files/{file_id}/content",
    {
      params: {
        path: { file_id: fileId, session_id: sessionId },
        query,
      },
      signal: options.signal,
    },
  );
  return {
    ...response,
    file: toSessionFile(response.file),
  };
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
