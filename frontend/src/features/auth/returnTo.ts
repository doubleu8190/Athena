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
    /[\u0000-\u001f]/.test(candidate) ||
    /^\/(?:login|register)(?:[/?#]|$)/.test(candidate)
  ) {
    return "/chat";
  }
  return candidate;
}
