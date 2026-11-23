import { ApiError } from "../../shared/api/client";

const FILE_ERROR_COPY: Record<string, string> = {
  session_not_found: "会话不存在或已不可访问。",
  session_file_not_found: "文件不存在、已被移除或不属于当前会话。",
  document_file_too_large: "文件超过 50 MiB 单文件上限。",
  file_name_invalid: "文件名不安全、包含控制字符或格式不合法。",
  file_type_unsupported: "文件类型不在允许的会话资料范围内。",
  file_content_mismatch: "文件内容与扩展名或声明类型不一致。",
  file_mime_mismatch: "文件 MIME 类型不在允许范围内。",
  file_content_not_text: "该文件不是可按文本读取的 UTF-8 文件。",
  session_file_quota_exceeded: "当前会话已达到 100 个文件上限，请创建新会话继续。",
  session_file_range_invalid: "文件读取范围无效，请从新的偏移重新读取。",
  session_file_ids_duplicate: "文件选择包含重复 ID，请刷新后重新选择。",
  file_upload_invalid: "文件上传参数无效。",
  file_storage_unavailable: "文件存储暂时不可用，请稍后重试。",
  idempotency_conflict: "同一请求标识已用于不同内容，请重新选择文件后再试。",
  idempotency_key_invalid: "文件请求标识无效，请刷新页面后重试。",
  network_error: "无法连接服务器，请检查网络后重试。",
  http_error: "服务器拒绝了本次文件请求，请稍后重试。",
  invalid_response: "服务器返回了无法识别的文件响应。",
};

export function fileErrorMessage(error: unknown): string {
  if (
    error instanceof ApiError &&
    Object.hasOwn(FILE_ERROR_COPY, error.detail)
  ) {
    return FILE_ERROR_COPY[error.detail];
  }
  return "文件操作未完成，请稍后重试。";
}

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
