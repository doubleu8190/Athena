import type { ApiSchemas } from "../../shared/api/client";
import { countCodePoints } from "../../shared/lib/text";

export const SYSTEM_PROMPT_CONTENT_MAX_LENGTH = 100_000;
export const DEFAULT_HTTPS_URL_MAX_LENGTH = 2_048;
export const CONTROL_CHARACTER_PATTERN = /\p{Cc}/u;

// 直连 OpenAPI 系统提示词与组织部门契约，及管理端公共分页与校验类型。
export type SystemPromptKind = ApiSchemas["SystemPromptKind"];
export type SystemPromptVersion = ApiSchemas["SystemPromptVersionResponse"];
export type SystemPromptVersionSummary =
  ApiSchemas["SystemPromptVersionSummary"];
export type SystemPromptVersionListResponse =
  ApiSchemas["SystemPromptVersionListResponse"];
export type OrgDepartment = ApiSchemas["DepartmentResponse"];
export type OrgDepartmentListResponse = ApiSchemas["DepartmentListResponse"];

export interface AdminPageQuery {
  page: number;
  pageSize: number;
}

export type FieldErrors<Field extends string> = Partial<
  Record<Field, string>
>;

/** 按 Unicode 码点计算字符串长度，与后端 Python `len()` 保持一致。 */
export function unicodeLength(value: string): number {
  return countCodePoints(value);
}

/** 解析非负十进制整数文本，非法或越界返回 null。 */
export function parseInteger(value: string): number | null {
  const normalized = value.trim();
  if (!/^\d+$/.test(normalized)) {
    return null;
  }
  const parsed = Number(normalized);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

/** 解析有限浮点比例文本，空串或非法数字返回 null。 */
export function parseRatio(value: string): number | null {
  const normalized = value.trim();
  if (!normalized) {
    return null;
  }
  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}

/** 校验系统提示词正文非全空白且不超过 Unicode 字符上限。 */
export function validateSystemPromptContent(content: string): string | null {
  if (!content.trim()) {
    return "系统提示词不能全部为空白。";
  }
  if (unicodeLength(content) > SYSTEM_PROMPT_CONTENT_MAX_LENGTH) {
    return `系统提示词不能超过 ${SYSTEM_PROMPT_CONTENT_MAX_LENGTH.toLocaleString()} 个字符。`;
  }
  return null;
}

/** 校验纯 HTTPS 端点 URL（禁止凭据、查询参数与片段）。 */
export function validateHttpsUrl(
  value: string,
  maxLength = DEFAULT_HTTPS_URL_MAX_LENGTH,
): string | null {
  const normalized = value.trim();
  if (!normalized || normalized.length < 8) {
    return "请输入完整的 HTTPS 地址。";
  }
  if (normalized.length > maxLength) {
    return `地址不能超过 ${maxLength} 个字符。`;
  }
  try {
    const parsed = new URL(normalized);
    if (parsed.protocol !== "https:") {
      return "地址必须使用 HTTPS。";
    }
    if (
      !parsed.hostname ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash
    ) {
      return "地址不能包含账号、查询参数或片段。";
    }
    return null;
  } catch {
    return "地址格式无效。";
  }
}
