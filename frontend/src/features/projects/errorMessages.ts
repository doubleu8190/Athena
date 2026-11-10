import { ApiError } from "../../shared/api/client";

const PROJECT_ERROR_COPY: Record<string, string> = {
  project_name_invalid: "项目名称需为 1–120 个字符，且不能包含控制字符。",
  project_has_sessions: "该项目仍有会话，请先删除全部会话后再删除项目。",
  project_not_found: "项目不存在或已被删除。",
  network_error: "无法连接服务器，请检查网络后重试。",
  http_error: "服务器拒绝了本次请求，请稍后重试。",
  invalid_response: "服务器返回了无法识别的响应，请稍后重试。",
};

export function projectErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return PROJECT_ERROR_COPY[error.detail] ?? "项目操作未完成，请稍后重试。";
  }
  return "项目操作未完成，请稍后重试。";
}

export function isProjectNotFound(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.status === 404 || error.detail === "project_not_found")
  );
}

