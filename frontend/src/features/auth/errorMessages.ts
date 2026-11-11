import { ApiError } from "../../shared/api/client";

const AUTH_ERROR_COPY: Record<string, string> = {
  invalid_credentials: "邮箱或密码不正确。",
  email_exists: "该邮箱已注册，请直接登录。",
  password_invalid: "密码需为 12–128 个字符。",
  current_password_invalid: "当前密码不正确。",
  invalid_email: "请输入有效的邮箱地址。",
  display_name_invalid: "显示名最多 80 个字符，且不能包含换行等控制字符。",
  rate_limited: "尝试次数过多，请稍后再试。",
  origin_not_allowed: "当前页面来源不允许提交请求，请确认前后端同源访问。",
  not_authenticated: "登录状态已失效，请重新登录。",
  network_error: "无法连接服务器，请确认后端已启动后重试。",
  invalid_response: "服务器返回了无法识别的响应，请稍后重试。",
};

export function authErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return AUTH_ERROR_COPY[error.detail] ?? "请求未完成，请稍后再试。";
  }
  return "请求未完成，请稍后再试。";
}
