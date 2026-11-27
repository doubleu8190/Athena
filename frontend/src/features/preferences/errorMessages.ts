import { ApiError } from "../../shared/api/client";
import { createApiErrorMessage } from "../../shared/api/errorMessage";

const PREFERENCE_ERROR_COPY: Record<string, string> = {
  custom_instructions_invalid:
    "长期工作指令最多 4,000 个字符，且不能包含不支持的控制字符。",
  user_memory_content_invalid:
    "备忘内容需为 1–500 个字符，且不能包含不支持的控制字符。",
  user_memory_limit_reached:
    "显式备忘已达上限，请先删除旧备忘后再添加。",
  user_memory_not_found: "该条备忘不存在或已被删除。",
};

export const preferenceErrorMessage = createApiErrorMessage(
  PREFERENCE_ERROR_COPY,
  "操作未完成，请稍后重试。",
);

export function isMemoryLimitReachedError(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 409 &&
    error.detail === "user_memory_limit_reached"
  );
}
