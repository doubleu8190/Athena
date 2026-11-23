import { ApiError } from "../../shared/api/client";

const CHAT_ERROR_COPY: Record<string, string> = {
  run_already_active: "该会话已有运行中的任务，请等待完成或先停止。",
  run_request_id_conflict:
    "请求标识已被另一条内容占用，请检查内容后重新发送。",
  run_request_id_invalid: "请求标识无效，请刷新页面后重新发送。",
  run_text_invalid: "消息内容为空或超过 32,000 个字符。",
  run_skill_ids_invalid: "技能选择包含重复、空白或无效的技能 ID。",
  run_input_file_ids_invalid: "文件选择包含重复、空白或无效的文件 ID。",
  run_input_file_ids_duplicate: "文件选择包含重复 ID，请刷新后重新选择。",
  run_input_file_unavailable:
    "所选文件已失效或不属于当前会话，请刷新文件列表后重试。",
  run_input_request_not_pending:
    "该补充输入请求已失效，正在同步最新运行状态。",
  run_not_found: "运行记录不存在或已被删除。",
  session_not_found: "会话不存在或已被删除。",
  run_not_retryable: "只有最新失败的任务可以重试。",
  run_not_latest: "已有更新的任务，不能重试该运行。",
  run_tool_approval_not_pending: "该工具调用已不在待审批状态。",
  run_tool_approval_already_decided: "该工具调用已经完成审批。",
  skill_selection_invalid:
    "技能选择重复或包含当前 Run 不支持的项目，请重新选择。",
  skill_selection_unavailable:
    "所选技能已停用、依赖失效或版本不可用，请刷新目录后重新选择。",
  skill_catalog_integrity_invalid:
    "技能目录数据不完整，当前不能安全创建 Run，请稍后重试。",
  skill_approval_projection_unavailable:
    "该技能审批缺少安全投影，服务端只允许拒绝。请刷新后重试。",
  skill_approval_metadata_unavailable:
    "技能审批元数据已失效，服务端只允许拒绝。请刷新后重试。",
  session_file_quota_exceeded:
    "当前会话已达到文件数量上限，请创建新会话继续。",
  file_storage_unavailable: "文件存储暂时不可用，请稍后重试。",
  tool_retry_confirmation_required: "重试可能重复已成功操作，请确认后重试。",
  project_not_found: "指定项目不存在或不可访问。",
  knowledge_base_not_found: "所选知识库不存在或当前无权访问，请重新选择。",
  run_knowledge_base_ids_invalid:
    "知识库选择包含无效、重复或超过 20 个上限的项，请重新选择。",
  knowledge_base_ids_invalid: "所选知识库范围无效，请重新选择。",
  run_model_profile_id_invalid: "所选模型标识无效，请重新选择模型后重试。",
  model_profile_unavailable: "所选模型当前不可用，请切换其他模型后重试。",
  network_error: "无法连接服务器，请检查网络后重试。",
  http_error: "服务器拒绝了本次请求，请稍后重试。",
  invalid_response: "服务器返回了无法识别的响应，请稍后重试。",
};

export function chatErrorMessage(error: unknown): string {
  if (
    error instanceof ApiError &&
    Object.hasOwn(CHAT_ERROR_COPY, error.detail)
  ) {
    return CHAT_ERROR_COPY[error.detail];
  }
  return "运行操作未完成，请稍后重试。";
}

export function isNetworkError(error: unknown): boolean {
  return error instanceof ApiError && error.detail === "network_error";
}

export function requestIdCanBeReused(error: unknown): boolean {
  if (!(error instanceof ApiError)) {
    return false;
  }
  // 网络失败、服务端 5xx 或成功响应无法解析时，服务端可能已经提交命令，
  // 下一次重试必须复用同一 request_id，避免重复创建运行。
  return (
    isNetworkError(error) ||
    error.status >= 500 ||
    error.detail === "invalid_response"
  );
}


