const MAX_RETURN_TO_LENGTH = 1024;

/**
 * 校验登录与 OAuth 授权后的站内回跳路径。
 * 严格拒绝外链、协议相对路径（//evil.com）、反斜杠、控制字符以及 /login 自身路径，
 * 防止开放重定向漏洞与登录页死循环。
 */
export function sanitizeReturnTo(candidate: unknown): string {
  if (typeof candidate !== "string" || !candidate) {
    return "/chat";
  }
  if (
    candidate.length > MAX_RETURN_TO_LENGTH ||
    !candidate.startsWith("/") ||
    candidate.startsWith("//") ||
    candidate.includes("\\") ||
    // 显式拦截 C0 控制字符与 DEL，防止跳转地址注入换行或非法控制序列。
    // eslint-disable-next-line no-control-regex
    /[\u0000-\u001f\u007f]/.test(candidate) ||
    /^\/login(?:[/?#]|$)/i.test(candidate)
  ) {
    return "/chat";
  }
  return candidate;
}

/**
 * 按优先级提取安全的站内回跳路径：优先使用路由 state.returnTo，其次回退 URL ?return_to= 参数。
 */
export function getSafeReturnTo(state: unknown, search?: string): string {
  if (typeof state === "object" && state !== null && "returnTo" in state) {
    const fromState = (state as { returnTo?: unknown }).returnTo;
    if (typeof fromState === "string") {
      return sanitizeReturnTo(fromState);
    }
  }
  if (search) {
    const fromQuery = new URLSearchParams(search).get("return_to");
    if (fromQuery !== null) {
      return sanitizeReturnTo(fromQuery);
    }
  }
  return "/chat";
}
