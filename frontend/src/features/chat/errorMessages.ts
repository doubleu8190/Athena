import { ApiError } from "../../shared/api/client";

const CHAT_ERROR_COPY: Record<string, string> = {
  run_already_active: "该会话已有运行中的任务，请等待完成或先停止。",
  run_request_id_conflict:
    "请求标识已被另一条内容占用，请检查内容后重新发送。",
  run_text_invalid: "消息内容为空或超过 32,000 个字符。",
  run_input_request_not_pending:
    "该补充输入请求已失效，正在同步最新运行状态。",
  run_not_found: "运行记录不存在或已被删除。",
  session_not_found: "会话不存在或已被删除。",
  run_not_retryable: "只有最新失败的任务可以重试。",
  run_not_latest: "已有更新的任务，不能重试该运行。",
  run_tool_approval_not_pending: "该工具调用已不在待审批状态。",
  run_tool_approval_already_decided: "该工具调用已经完成审批。",
  tool_retry_confirmation_required: "重试可能重复已成功操作，请确认后重试。",
  project_not_found: "指定项目不存在或不可访问。",
  network_error: "无法连接服务器，请检查网络后重试。",
  http_error: "服务器拒绝了本次请求，请稍后重试。",
  invalid_response: "服务器返回了无法识别的响应，请稍后重试。",
};

export function chatErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return CHAT_ERROR_COPY[error.detail] ?? "运行操作未完成，请稍后重试。";
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


