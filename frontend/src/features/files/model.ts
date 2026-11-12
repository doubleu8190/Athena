export const SESSION_FILE_MAX_BYTES = 50 * 1024 * 1024;
export const SESSION_FILE_PAGE_SIZE = 50;

export type SessionFileSource = "upload" | "artifact";

export interface SessionFile {
  id: string;
  session_id: string;
  source: SessionFileSource;
  filename: string;
  label: string | null;
  mime_type: string;
  size_bytes: number;
  created_at: string;
}

export interface SessionFilePage {
  items: SessionFile[];
  next_after_file_id: string | null;
}

export interface ArtifactFileRef {
  file_id: string;
  filename: string;
  label: string | null;
  mime_type: string;
  size_bytes: number;
}

export interface PendingSessionUpload {
  file: File;
  requestId: string;
  reuseRequestId: boolean;
}

const BLOCKED_FILE_EXTENSIONS = new Set([
  "app",
  "bat",
  "cmd",
  "com",
  "dll",
  "dmg",
  "exe",
  "html",
  "htm",
  "js",
  "mjs",
  "msi",
  "php",
  "ps1",
  "py",
  "sh",
  "svg",
  "vbs",
  "zip",
]);

export function fileExtension(filename: string): string {
  const index = filename.lastIndexOf(".");
  return index >= 0 ? filename.slice(index + 1).toLowerCase() : "";
}

export function validateSessionUpload(file: File): string | null {
  if (file.size === 0) {
    return `“${file.name || "未命名文件"}”是空文件，请选择有内容的资料。`;
  }
  if (file.size > SESSION_FILE_MAX_BYTES) {
    return `“${file.name}”超过 50 MiB 单文件上限。`;
  }
  if (!file.name.trim() || BLOCKED_FILE_EXTENSIONS.has(fileExtension(file.name))) {
    return `“${file.name || "未命名文件"}”不属于允许上传的普通资料类型。`;
  }
  return null;
}

export function fileDisplayName(
  file: Pick<SessionFile | ArtifactFileRef, "filename" | "label">,
): string {
  return file.label?.trim() || file.filename;
}
