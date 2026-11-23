export function getSafeReturnTo(state: unknown): string {
  if (typeof state !== "object" || state === null || !("returnTo" in state)) {
    return "/chat";
  }
  const candidate = state.returnTo;
  if (typeof candidate !== "string") {
    return "/chat";
  }
  if (
    !candidate.startsWith("/") ||
    candidate.startsWith("//") ||
    candidate.includes("\\") ||
    // 显式拦截 C0 控制字符，防止跳转地址注入换行或非法控制序列。
    // eslint-disable-next-line no-control-regex
    /[\u0000-\u001f]/.test(candidate) ||
    /^\/(?:login|register)(?:[/?#]|$)/.test(candidate)
  ) {
    return "/chat";
  }
  return candidate;
}
