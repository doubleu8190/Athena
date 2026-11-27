import { ApiError } from "../../shared/api/client";
import { createApiErrorMessage } from "../../shared/api/errorMessage";

const SESSION_ERROR_COPY: Record<string, string> = {
  session_title_invalid: "会话标题需为 1–120 个字符，且不能包含控制字符。",
  session_update_invalid: "没有提供可更新的会话字段。",
  session_project_locked: "首条消息发送后，会话不能再更改所属项目。",
  session_has_active_run: "当前对话有正在执行的任务，请等待完成或停止后再操作。",
  session_not_found: "会话不存在或已被删除。",
  project_not_found: "指定项目不存在或已被删除。",
};

export const sessionErrorMessage = createApiErrorMessage(
  SESSION_ERROR_COPY,
  "会话操作未完成，请稍后重试。",
);

export function isSessionNotFound(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.status === 404 || error.detail === "session_not_found")
  );
}
