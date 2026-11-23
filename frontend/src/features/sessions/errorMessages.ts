import { ApiError } from "../../shared/api/client";

const SESSION_ERROR_COPY: Record<string, string> = {
  session_title_invalid: "会话标题需为 1–120 个字符，且不能包含控制字符。",
  session_update_invalid: "没有提供可更新的会话字段。",
  session_project_locked: "首条消息发送后，会话不能再更改所属项目。",
  session_has_active_run: "当前对话有正在执行的任务，请等待完成或停止后再操作。",
  session_not_found: "会话不存在或已被删除。",
  project_not_found: "指定项目不存在或已被删除。",
  network_error: "无法连接服务器，请检查网络后重试。",
  http_error: "服务器拒绝了本次请求，请稍后重试。",
  invalid_response: "服务器返回了无法识别的响应，请稍后重试。",
};

const DEFAULT_SESSION_ERROR = "会话操作未完成，请稍后重试。";

export function sessionErrorMessage(error: unknown): string {
  if (
    error instanceof ApiError &&
    Object.hasOwn(SESSION_ERROR_COPY, error.detail)
  ) {
    return SESSION_ERROR_COPY[error.detail];
  }
  return DEFAULT_SESSION_ERROR;
}

export function isSessionNotFound(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.status === 404 || error.detail === "session_not_found")
  );
}

