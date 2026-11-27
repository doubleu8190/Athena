import { fileExtension } from "../../shared/lib/file";

export const SESSION_FILE_MAX_BYTES = 50 * 1024 * 1024;
export const SESSION_FILE_PAGE_SIZE = 50;
export const SESSION_FILE_CONTENT_DEFAULT_LIMIT_BYTES = 65_536;
export const MAX_VISION_IMAGES_PER_RUN = 5;

export type SessionFileSource = "upload" | "artifact";

/** 办公文档自动提取缓存状态，与后端 SessionFileResponse 严格对齐。 */
export type SessionFileExtractionStatus = "none" | "ready" | "failed";

/** 文本读取接口返回的内容来源类型：纯文本原件或办公文档提取缓存。 */
export type SessionFileContentSourceKind = "raw_text" | "extracted_document";

export interface SessionFile {
  id: string;
  session_id: string;
  source: SessionFileSource;
  filename: string;
  label: string | null;
  mime_type: string;
  size_bytes: number;
  extraction_status: SessionFileExtractionStatus;
  extracted_text_bytes: number | null;
  extraction_error_code: string | null;
  created_at: string;
}

export interface SessionFilePage {
  items: SessionFile[];
  next_after_file_id: string | null;
}

export interface SessionFileContentResponse {
  file: SessionFile;
  source_kind: SessionFileContentSourceKind;
  offset: number;
  next_offset: number;
  content: string;
  eof: boolean;
  truncated: boolean;
}

export interface ArtifactFileRef {
  file_id: string;
  filename: string;
  label: string | null;
  mime_type: string;
  size_bytes: number;
}

/** 统一文件在线预览与原图灯箱目标元数据（消息气泡、产物卡片、输入器与文件抽屉共用）。 */
export interface FilePreviewTarget {
  fileId: string;
  filename: string;
  label?: string | null;
  mimeType?: string | null;
  sizeBytes?: number | null;
  extractionStatus?: SessionFileExtractionStatus;
  extractedTextBytes?: number | null;
  extractionErrorCode?: string | null;
}

export type FilePreviewKind =
  | "image"
  | "markdown"
  | "csv"
  | "json"
  | "text"
  | "document"
  | "unsupported";

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

/** 后端 /preview 端点严格放行的安全位图 MIME 白名单（严禁包含 SVG/HTML）。 */
const PREVIEWABLE_IMAGE_MIMES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
]);

const PREVIEWABLE_IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "webp"]);

/** 存在同源脚本执行风险的 Web/矢量扩展名，绝不允许进入安全内联预览。 */
const UNSAFE_INLINE_EXTENSIONS = new Set([
  "svg",
  "svgz",
  "html",
  "htm",
  "xhtml",
  "xml",
  "js",
  "mjs",
]);

/** 存在同源脚本执行或内容嗅探风险的 MIME 类型，绝不允许进入安全内联预览。 */
const UNSAFE_INLINE_MIMES = new Set([
  "image/svg+xml",
  "text/html",
  "application/xhtml+xml",
  "application/xml",
  "text/xml",
  "text/javascript",
  "application/javascript",
]);

const GENERIC_BINARY_MIMES = new Set([
  "application/octet-stream",
  "binary/octet-stream",
]);

const EXTRACTABLE_OFFICE_EXTENSIONS = new Set(["pdf", "docx", "xlsx", "pptx"]);

const EXTRACTABLE_OFFICE_MIMES = new Set([
  "application/pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
]);

const PREVIEWABLE_TEXT_EXTENSIONS = new Set([
  "txt",
  "md",
  "csv",
  "json",
  "log",
]);

const PREVIEWABLE_TEXT_MIMES = new Set([
  "text/plain",
  "text/markdown",
  "text/csv",
  "application/csv",
  "application/json",
]);

function normalizeMimeType(mimeType?: string | null): string {
  if (!mimeType) {
    return "";
  }
  return mimeType.split(";")[0]?.trim().toLowerCase() ?? "";
}

function isGenericBinaryMime(mime: string): boolean {
  return GENERIC_BINARY_MIMES.has(mime);
}

/**
 * 合并消息片段与会话文件缓存中的双来源 MIME：
 * 只要任一来源声明高危不安全 MIME（如 image/svg+xml、text/html），立即返回该不安全 MIME 以触发拦截；
 * 否则优先选用非空且非通用二进制的 MIME，避免缺省 MIME 掩盖真实高危类型。
 */
export function mergeSafeFileMimeType(
  primary?: string | null,
  secondary?: string | null,
): string | null {
  const primaryNorm = normalizeMimeType(primary);
  const secondaryNorm = normalizeMimeType(secondary);
  if (UNSAFE_INLINE_MIMES.has(primaryNorm)) {
    return primary ?? primaryNorm;
  }
  if (UNSAFE_INLINE_MIMES.has(secondaryNorm)) {
    return secondary ?? secondaryNorm;
  }
  if (primaryNorm && !isGenericBinaryMime(primaryNorm)) {
    return primary ?? primaryNorm;
  }
  if (secondaryNorm && !isGenericBinaryMime(secondaryNorm)) {
    return secondary ?? secondaryNorm;
  }
  return primary ?? secondary ?? null;
}

/**
 * 判断文件是否属于后端安全白名单允许内联展示的位图（PNG / JPEG / WEBP）：
 * 执行扩展名与 MIME 双白名单严格交叉校验，显式阻断 SVG、HTML、XML 与伪装扩展名/MIME 组合，防止同源 XSS。
 */
export function isPreviewableImageFile(
  filename: string,
  mimeType?: string | null,
): boolean {
  const ext = fileExtension(filename);
  const mime = normalizeMimeType(mimeType);
  if (UNSAFE_INLINE_EXTENSIONS.has(ext) || UNSAFE_INLINE_MIMES.has(mime)) {
    return false;
  }
  const hasKnownMime = Boolean(mime) && !isGenericBinaryMime(mime);
  // 扩展名非空时必须命中安全位图白名单；明确携带非通用二进制 MIME 时也必须命中安全位图 MIME 白名单。
  if (ext && !PREVIEWABLE_IMAGE_EXTENSIONS.has(ext)) {
    return false;
  }
  if (hasKnownMime && !PREVIEWABLE_IMAGE_MIMES.has(mime)) {
    return false;
  }
  return (
    PREVIEWABLE_IMAGE_EXTENSIONS.has(ext) ||
    (hasKnownMime && PREVIEWABLE_IMAGE_MIMES.has(mime))
  );
}

/**
 * 判断文件是否属于后端支持自动结构化文本提取的办公文档（.pdf / .docx / .xlsx / .pptx）：
 * 严格约束扩展名与 MIME 一致性，禁止旧版 .doc/.xls/.ppt 或图片/高危 MIME 交叉伪装。
 */
export function isExtractableOfficeFile(
  filename: string,
  mimeType?: string | null,
): boolean {
  const ext = fileExtension(filename);
  const mime = normalizeMimeType(mimeType);
  if (
    UNSAFE_INLINE_EXTENSIONS.has(ext) ||
    UNSAFE_INLINE_MIMES.has(mime) ||
    mime.startsWith("image/")
  ) {
    return false;
  }
  const hasKnownMime = Boolean(mime) && !isGenericBinaryMime(mime);
  if (ext) {
    if (!EXTRACTABLE_OFFICE_EXTENSIONS.has(ext)) {
      return false;
    }
    // 兼容部分操作系统将 OOXML（docx/xlsx/pptx）声明为 application/zip，其余非通用二进制 MIME 必须命中白名单。
    if (
      hasKnownMime &&
      !EXTRACTABLE_OFFICE_MIMES.has(mime) &&
      mime !== "application/zip"
    ) {
      return false;
    }
    return true;
  }
  return hasKnownMime && EXTRACTABLE_OFFICE_MIMES.has(mime);
}

/** 判断文件是否属于可直接通过 /content 读取的纯文本白名单（.txt / .md / .csv / .json / .log）。 */
export function isPreviewableTextFile(
  filename: string,
  mimeType?: string | null,
): boolean {
  const ext = fileExtension(filename);
  const mime = normalizeMimeType(mimeType);
  if (
    UNSAFE_INLINE_EXTENSIONS.has(ext) ||
    UNSAFE_INLINE_MIMES.has(mime) ||
    mime.startsWith("image/")
  ) {
    return false;
  }
  const hasKnownMime = Boolean(mime) && !isGenericBinaryMime(mime);
  if (ext) {
    if (!PREVIEWABLE_TEXT_EXTENSIONS.has(ext)) {
      return false;
    }
    // 若同时携带非通用二进制 MIME，禁止 pdf/office/可执行等非文本 MIME 伪装（兼容 Windows 将 .csv 标记为 application/vnd.ms-excel）。
    if (
      hasKnownMime &&
      !PREVIEWABLE_TEXT_MIMES.has(mime) &&
      !mime.startsWith("text/") &&
      !(ext === "csv" && mime === "application/vnd.ms-excel")
    ) {
      return false;
    }
    return true;
  }
  return hasKnownMime && PREVIEWABLE_TEXT_MIMES.has(mime);
}

/** 判断文件是否支持在前端弹窗内安全在线预览（白名单位图、纯文本或可提取办公文档）。 */
export function canInlinePreviewFile(
  filename: string,
  mimeType?: string | null,
): boolean {
  return (
    isPreviewableImageFile(filename, mimeType) ||
    isPreviewableTextFile(filename, mimeType) ||
    isExtractableOfficeFile(filename, mimeType)
  );
}

/** 解析文件对应的在线预览渲染形态。 */
export function resolveFilePreviewKind(
  filename: string,
  mimeType?: string | null,
): FilePreviewKind {
  if (isPreviewableImageFile(filename, mimeType)) {
    return "image";
  }
  if (isExtractableOfficeFile(filename, mimeType)) {
    return "document";
  }
  if (isPreviewableTextFile(filename, mimeType)) {
    const ext = fileExtension(filename);
    const mime = normalizeMimeType(mimeType);
    if (ext === "md" || (!ext && mime === "text/markdown")) {
      return "markdown";
    }
    if (ext === "csv" || (!ext && (mime === "text/csv" || mime === "application/csv"))) {
      return "csv";
    }
    if (ext === "json" || (!ext && mime === "application/json")) {
      return "json";
    }
    return "text";
  }
  return "unsupported";
}

/** 构造同源 Cookie 鉴权直出的安全位图预览 URL，供 <img> 缩略图与灯箱直接加载。 */
export function sessionFilePreviewUrl(
  sessionId: string,
  fileId: string,
): string {
  return `/api/v1/sessions/${encodeURIComponent(sessionId)}/files/${encodeURIComponent(fileId)}/preview`;
}

/** 将 SessionFile 转换为统一预览弹窗目标对象。 */
export function sessionFileToPreviewTarget(
  file: SessionFile,
): FilePreviewTarget {
  return {
    extractedTextBytes: file.extracted_text_bytes,
    extractionErrorCode: file.extraction_error_code,
    extractionStatus: file.extraction_status,
    fileId: file.id,
    filename: file.filename,
    label: file.label,
    mimeType: file.mime_type,
    sizeBytes: file.size_bytes,
  };
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
