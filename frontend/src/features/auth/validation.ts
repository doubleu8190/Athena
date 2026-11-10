const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const CONTROL_CHARACTER_PATTERN = /[\u0000-\u001f\u007f]/;

export interface AuthFieldErrors {
  displayName?: string;
  email?: string;
  form?: string;
  password?: string;
}

export function validateEmail(email: string): string | undefined {
  if (!email) {
    return "请输入工作邮箱。";
  }
  if (!EMAIL_PATTERN.test(email) || email.length > 254) {
    return "请输入有效的邮箱地址。";
  }
  return undefined;
}

export function validatePassword(password: string): string | undefined {
  if (!password) {
    return "请输入密码。";
  }
  // Python len 按 Unicode 码点计数；Array.from 避免非 BMP 字符被当成两个 UTF-16 单元。
  const codePointLength = Array.from(password).length;
  if (codePointLength < 12 || codePointLength > 128) {
    return "密码需为 12–128 个字符。";
  }
  return undefined;
}

export function validateDisplayName(
  displayName: string,
): string | undefined {
  const normalized = displayName.trim();
  if (!normalized) {
    return undefined;
  }
  if (
    Array.from(normalized).length > 80 ||
    CONTROL_CHARACTER_PATTERN.test(normalized)
  ) {
    return "显示名最多 80 个字符，且不能包含换行等控制字符。";
  }
  return undefined;
}
