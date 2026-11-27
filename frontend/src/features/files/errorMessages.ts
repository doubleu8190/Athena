import { ApiError } from "../../shared/api/client";
import {
  createApiErrorMessage,
  lookupErrorCopy,
} from "../../shared/api/errorMessage";

const FILE_ERROR_COPY: Record<string, string> = {
  session_not_found: "会话不存在或已不可访问。",
  session_file_not_found: "文件不存在、已被移除或不属于当前会话。",
  document_file_too_large: "文件超过 50 MiB 单文件上限。",
  file_name_invalid: "文件名不安全、包含控制字符或格式不合法。",
  file_type_unsupported: "文件类型不在允许的会话资料范围内。",
  file_content_mismatch: "文件内容与扩展名或声明类型不一致。",
  file_mime_mismatch: "文件 MIME 类型不在允许范围内。",
  file_preview_unsupported:
    "该文件类型不支持安全内联图片预览，请下载原件或以文本方式查看。",
  file_not_text: "该文件不是可在线读取的纯文本或受支持办公文档，请下载原件查看。",
  file_offset_invalid: "文本分页读取偏移量无效，请重新打开预览从头读取。",
  file_content_not_text: "该文件不是可按文本读取的 UTF-8 文件，请下载原件查看。",
  file_range_too_small: "本次读取字节范围过小，请重试加载。",
  document_extract_too_large:
    "原文档超过 20 MiB 自动提取上限，暂不支持在线预览文本，请下载原件查看。",
  document_encrypted:
    "文档受密码加密保护，无法自动提取文本，请下载原件查看。",
  document_macro_unsupported:
    "该办公文档包含宏（VBA），受安全策略限制无法自动提取，请下载原件查看。",
  document_page_limit_exceeded:
    "PDF 页数超过 500 页自动提取上限，无法在线预览，请下载原件查看。",
  document_archive_limit_exceeded:
    "办公文档压缩结构超出安全限制，无法自动提取，请下载原件查看。",
  document_extracted_text_too_large:
    "提取后的文本超过 10 MiB 缓存上限，无法在线预览，请下载原件查看。",
  document_no_extractable_text:
    "扫描件或空白文档，暂无可提取文本（可截图识图或下载原件）。",
  document_type_unsupported:
    "暂不支持自动提取该旧版或未知办公格式，请转换格式或下载原件查看。",
  document_extract_timeout:
    "文档解析超过 15 秒超时上限，暂不支持在线预览，请下载原件查看。",
  document_parse_failed:
    "文档可能已损坏或格式异常，无法自动提取文本，请下载原件查看。",
  session_file_quota_exceeded: "当前会话已达到 100 个文件上限，请创建新会话继续。",
  session_file_range_invalid: "文件读取范围无效，请从新的偏移重新读取。",
  session_file_ids_duplicate: "文件选择包含重复 ID，请刷新后重新选择。",
  file_upload_invalid: "文件上传参数无效。",
  file_storage_unavailable: "文件存储暂时不可用，请稍后重试。",
  idempotency_conflict: "同一请求标识已用于不同内容，请重新选择文件后再试。",
  idempotency_key_invalid: "文件请求标识无效，请刷新页面后重试。",
  http_error: "服务器拒绝了本次文件请求，请稍后重试。",
  invalid_response: "服务器返回了无法识别的文件响应。",
};

/** 面向附件微标 Tooltip 与在线预览降级提示的温和中文提取失败说明。 */
const EXTRACTION_ERROR_SUMMARY: Record<string, string> = {
  document_extract_too_large:
    "原文档超过 20 MiB 自动提取上限（仍可正常发送或下载原件）",
  document_encrypted: "文档已加密保护，无法自动提取文本（仍可正常发送或下载原件）",
  document_macro_unsupported:
    "文档包含宏（VBA），安全策略已跳过提取（仍可正常发送或下载原件）",
  document_page_limit_exceeded:
    "PDF 超过 500 页提取上限（仍可正常发送或下载原件）",
  document_archive_limit_exceeded:
    "文档压缩结构超出安全限制，已跳过提取（仍可下载原件）",
  document_extracted_text_too_large:
    "提取文本超过 10 MiB 缓存上限，暂不提供在线预览（仍可下载原件）",
  document_no_extractable_text:
    "扫描件或空白文档，暂无可提取文本（可截图识图或下载原件）",
  document_type_unsupported:
    "暂不支持该办公格式的自动提取（可转换为 .pdf/.docx/.xlsx/.pptx 或下载原件）",
  document_extract_timeout:
    "文档结构复杂，解析超过 15 秒上限，已跳过提取（仍可下载原件）",
  document_parse_failed:
    "文档损坏或格式异常，未能提取文本（仍可下载原件）",
  file_storage_unavailable: "文件存储暂时不可用，请稍后重试预览",
};

/** 属于确定性文档提取/格式不可预览的错误码集合（无需自动引导重试，应直接引导下载原件）。 */
const DOCUMENT_EXTRACTION_ERROR_CODES = new Set(
  Object.keys(EXTRACTION_ERROR_SUMMARY).filter(
    (code) => code !== "file_storage_unavailable",
  ),
);

export function fileErrorCode(error: unknown): string | null {
  return error instanceof ApiError ? error.detail : null;
}

export function isDocumentExtractionErrorCode(
  code: string | null | undefined,
): boolean {
  return Boolean(code && DOCUMENT_EXTRACTION_ERROR_CODES.has(code));
}

export function extractionErrorSummary(
  code: string | null | undefined,
): string {
  return (
    (code ? lookupErrorCopy(EXTRACTION_ERROR_SUMMARY, code) : undefined) ??
    "暂未提取到可用文本（仍可正常发送或下载原件）"
  );
}

export const fileErrorMessage = createApiErrorMessage(
  FILE_ERROR_COPY,
  "文件操作未完成，请稍后重试。",
);

/**
 * 判断文件上传失败后是否应复用同一 Idempotency-Key：
 * 仅在网络中断（status === 0）、服务端 5xx 或响应无法解析（结果不确定）时复用原键，
 * 避免重复落库；明确的 4xx 客户端拒绝需换新键重试。
 */
export function fileRequestCanBeReused(error: unknown): boolean {
  if (error instanceof ApiError) {
    return (
      error.status === 0 ||
      error.status >= 500 ||
      error.detail === "invalid_response"
    );
  }
  if (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    "detail" in error
  ) {
    const status = Number(error.status);
    const detail = String(error.detail);
    return status === 0 || status >= 500 || detail === "invalid_response";
  }
  return false;
}
