import { ApiError } from "../../shared/api/client";

const KNOWLEDGE_ERROR_COPY: Record<string, string> = {
  knowledge_base_name_invalid:
    "知识库名称需为 1–120 个字符，且不能包含控制字符。",
  knowledge_base_description_invalid:
    "知识库说明最多 500 个字符，且不能包含换行或控制字符。",
  knowledge_base_patch_empty: "没有需要保存的变更。",
  knowledge_base_not_found: "知识库不存在、已删除，或不属于当前账号。",
  document_title_invalid:
    "文档标题需为 1–255 个字符，且不能包含控制字符。",
  document_text_invalid: "文本内容为空或超过 1,000,000 个字符。",
  document_not_found: "文档不存在或已删除。",
  version_not_found: "版本不存在或已删除。",
  version_not_failed: "只有失败的版本可以重试。",
  version_superseded: "该版本已被更新的版本替代，不能再重试。",
  source_not_found: "来源文件不存在，或当前版本没有可下载原件。",
  idempotency_conflict:
    "本次请求键与之前提交的内容不一致，请重新提交当前内容。",
  document_update_in_progress:
    "该文档已有版本正在处理中，请等待处理结束后再提交新版本。",
  document_type_unsupported:
    "不支持该文件格式。可上传 TXT、MD、PDF、DOCX、XLSX、PPTX、PNG、JPG、JPEG。",
  knowledge_file_type_unsupported:
    "不支持该文件格式。可上传 TXT、MD、PDF、DOCX、XLSX、PPTX、PNG、JPG、JPEG。",
  knowledge_legacy_office_unsupported:
    "不支持旧版 Office 文件，请转换为 DOCX、XLSX 或 PPTX 后上传。",
  document_file_too_large: "文件不能超过 20 MiB。",
  knowledge_file_size_invalid: "文件为空或超过 20 MiB。",
  knowledge_archive_expanded_size_limit: "压缩包解压后的内容过大。",
  knowledge_extracted_text_limit: "文档可提取文本超过后端限制。",
  knowledge_storage_quota_exceeded: "知识库存储配额已用尽，请删除部分资料后重试。",
  knowledge_document_quota_exceeded: "知识库文档数量配额已用尽，请删除部分文档后重试。",
  knowledge_storage_unavailable: "原件存储暂时不可用，请稍后重试。",
  knowledge_search_unavailable: "检索服务暂时不可用，请稍后重试。",
  search_request_invalid: "检索内容需为 1–4,000 个字符，数量限制为 1–20。",
  knowledge_base_ids_invalid: "知识库范围无效。",
  project_knowledge_limit_reached:
    "每个项目最多关联 20 个知识库，请先解除一个已有知识库。",
  project_not_found: "项目不存在或已删除。",
  not_authenticated: "登录状态已失效，请重新登录。",
  network_error: "无法连接服务器，请检查网络后重试。",
  http_error: "服务器拒绝了本次请求，请稍后重试。",
  invalid_response: "服务器返回了无法识别的响应，请稍后重试。",
};

const VERSION_ERROR_COPY: Record<string, string> = {
  knowledge_file_type_unsupported: "文件格式不受支持。",
  knowledge_legacy_office_unsupported: "旧版 Office 文件不受支持。",
  knowledge_file_signature_invalid: "文件内容与扩展名不匹配或文件已损坏。",
  knowledge_text_encoding_unsupported: "文本文件必须使用 UTF-8 编码。",
  knowledge_document_text_empty: "没有提取到可检索文本。",
  knowledge_document_invalid: "文档内容无法解析。",
  knowledge_pdf_page_limit: "PDF 页数超过后端限制。",
  knowledge_archive_expanded_size_limit: "Office 压缩包展开后过大。",
  knowledge_archive_member_limit: "Office 压缩包文件数量超过限制。",
  knowledge_archive_encrypted: "不支持加密的 Office 文件。",
  knowledge_office_macro_unsupported: "不支持包含宏的 Office 文件。",
  knowledge_parse_timeout: "解析超时。",
  knowledge_parser_transient: "解析服务暂时不可用，可尝试重试。",
  knowledge_ocr_engine_unavailable: "OCR 引擎暂不可用。",
  knowledge_storage_unavailable: "原件存储暂时不可用。",
  knowledge_processing_failed: "文档处理失败。",
};

export function knowledgeErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return (
      KNOWLEDGE_ERROR_COPY[error.detail] ??
      "知识库操作未完成，请稍后重试。"
    );
  }
  return "知识库操作未完成，请稍后重试。";
}

export function versionErrorMessage(code: string | null): string {
  if (!code) {
    return "处理失败，未返回具体原因。";
  }
  return VERSION_ERROR_COPY[code] ?? `处理失败（${code}）。`;
}

export function isKnowledgeNotFound(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.status === 404 ||
      [
        "knowledge_base_not_found",
        "document_not_found",
        "version_not_found",
      ].includes(error.detail))
  );
}

export function idempotencyKeyCanBeReused(error: unknown): boolean {
  if (!(error instanceof ApiError)) {
    return false;
  }
  return (
    error.status === 0 ||
    error.status >= 500 ||
    error.detail === "invalid_response"
  );
}
