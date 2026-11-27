import {
  COMMON_API_ERROR_COPY,
  createApiErrorMessage,
  lookupErrorCopy,
} from "../../shared/api/errorMessage";

const AUTH_ERROR_COPY: Record<string, string> = {
  invalid_credentials: "邮箱或密码不正确。",
  email_exists: "该邮箱已注册，请直接登录。",
  password_invalid: "密码需为 12–128 个字符。",
  current_password_invalid: "当前密码不正确。",
  password_change_not_supported:
    "企业单点登录账号由企业统一管理密码，不支持在此修改。",
  invalid_email: "请输入有效的邮箱地址。",
  display_name_invalid: "显示名最多 80 个字符，且不能包含换行等控制字符。",
  rate_limited: "尝试次数过多，请稍后再试。",
  rate_limit_unavailable: "安全限流服务暂不可用，请稍后再试。",
  origin_not_allowed: "当前页面来源不允许提交请求，请确认前后端同源访问。",
  user_disabled: "该账号已被停用或已离职，无法登录。",
  feishu_not_enabled: "当前环境未启用飞书单点登录。",
  invalid_oauth_state: "登录校验已失效，请重新点击飞书登录。",
  invalid_oauth_callback: "飞书授权回调参数无效，请重新发起登录。",
  feishu_auth_failed: "飞书登录验证失败，请稍后重试或联系企业管理员。",
  oauth_state_unavailable: "登录校验服务暂不可用，请稍后再试。",
  mock_user_not_found: "所选体验账号不存在或已不可用。",
  auth_mock_not_enabled: "当前环境未启用本地快捷登录模式。",
  invalid_return_to: "跳转地址无效，请从登录页重新进入。",
  network_error: "无法连接服务器，请确认后端已启动后重试。",
};

export const authErrorMessage = createApiErrorMessage(
  {
    ...AUTH_ERROR_COPY,
    // 认证接口对未区分具体业务码的 5xx/非 JSON 响应维持既有通用提示文案。
    http_error: "请求未完成，请稍后再试。",
  },
  "请求未完成，请稍后再试。",
);

/**
 * 将登录页 URL 查询参数（如 OAuth 回调重定向的 ?error=<code>）映射为受控中文文案，
 * 不直接回显未知错误原文，防止 URL 参数文案伪造或原型属性访问异常。
 */
export function authErrorCodeMessage(code: string | null): string | null {
  if (!code) {
    return null;
  }
  const normalized = code.trim();
  if (!normalized) {
    return null;
  }
  return (
    lookupErrorCopy(AUTH_ERROR_COPY, normalized) ??
    lookupErrorCopy(COMMON_API_ERROR_COPY, normalized) ??
    "登录未能完成，请重试或联系企业 IT 管理员。"
  );
}
