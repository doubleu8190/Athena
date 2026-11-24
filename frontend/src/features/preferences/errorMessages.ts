import { ApiError } from "../../shared/api/client";

const PREFERENCE_ERROR_COPY: Record<string, string> = {
  custom_instructions_invalid:
    "长期工作指令最多 4,000 个字符，且不能包含不支持的控制字符。",
  http_error: "服务器拒绝了本次请求，请稍后重试。",
  invalid_response: "服务器返回了无法识别的响应，请稍后重试。",
  network_error: "无法连接服务器，请检查网络后重试。",
  not_authenticated: "登录状态已失效，请重新登录。",
  user_memory_content_invalid:
    "备忘内容需为 1–500 个字符，且不能包含不支持的控制字符。",
  user_memory_limit_reached:
    "显式备忘已达上限，请先删除旧备忘后再添加。",
  user_memory_not_found: "该条备忘不存在或已被删除。",
};

function lookupPreferenceErrorCopy(code: string): string | undefined {
  // 严格通过 Object.hasOwn 查表，防止原型属性污染导致异常文本或渲染崩溃。
  return Object.hasOwn(PREFERENCE_ERROR_COPY, code)
    ? PREFERENCE_ERROR_COPY[code]
    : undefined;
}

export function preferenceErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return (
      lookupPreferenceErrorCopy(error.detail) ??
      "操作未完成，请稍后重试。"
    );
  }
  return "操作未完成，请稍后重试。";
}

export function isMemoryLimitReachedError(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 409 &&
    error.detail === "user_memory_limit_reached"
  );
}
