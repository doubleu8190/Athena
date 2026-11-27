import type { ApiSchemas } from "../../shared/api/client";
import { normalizeVisibilityGrants } from "../access/visibilityGrants";
import type { VisibilityGrantInput } from "../knowledge/model";
import { parseInteger, unicodeLength, type FieldErrors } from "./modelShared";

// 直连 OpenAPI Subagent 管理与可用工具目录契约。
export type SubagentProfile = ApiSchemas["SubagentResponse"];
export type SubagentVisibilityScope = SubagentProfile["visibility_scope"];
export type SubagentListResponse = ApiSchemas["SubagentListResponse"];
export type SubagentCreateInput = ApiSchemas["SubagentCreateRequest"];
export type SubagentUpdateInput = ApiSchemas["SubagentUpdateRequest"];
export type SubagentAvailableToolItem = ApiSchemas["AvailableToolItemResponse"];
export type SubagentAvailableToolGroup =
  ApiSchemas["AvailableToolGroupResponse"];
export type SubagentAvailableToolsResponse =
  ApiSchemas["SubagentAvailableToolsResponse"];

export interface SubagentFormValues {
  slug: string;
  name: string;
  description: string;
  system_prompt: string;
  model_profile_id: string;
  allowed_tool_ids: string[];
  enabled: boolean;
  max_llm_requests: string;
  timeout_seconds: string;
  visibility_scope: SubagentVisibilityScope;
  owner_department_id: string;
  visibility_grants: VisibilityGrantInput[];
}

export type SubagentField =
  | "slug"
  | "name"
  | "description"
  | "system_prompt"
  | "model_profile_id"
  | "allowed_tool_ids"
  | "max_llm_requests"
  | "timeout_seconds"
  | "owner_department_id";

export const SUBAGENT_SLUG_PATTERN = /^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$/;
export const MAX_SUBAGENT_NAME_LENGTH = 100;
export const MAX_SUBAGENT_DESCRIPTION_LENGTH = 1_000;
export const MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH = 32_000;
export const MAX_SUBAGENT_ALLOWED_TOOLS = 100;

export const RESERVED_SUBAGENT_SLUGS: ReadonlySet<string> = new Set([
  "default",
  "main",
  "subagent",
  "system",
]);

// 后端使用 Python str.strip()；它包含 Unicode White_Space 与 U+001C–U+001F，
// 但不把 U+FEFF 视为首尾空白，因此不能直接复用 JavaScript trim()。
const PYTHON_STRIP_PATTERN =
  // eslint-disable-next-line no-control-regex
  /^[\p{White_Space}\u001c-\u001f]+|[\p{White_Space}\u001c-\u001f]+$/gu;

function stripSubagentWhitespace(value: string): string {
  return value.replace(PYTHON_STRIP_PATTERN, "");
}

function hasDisallowedSubagentControlCharacter(
  value: string,
  allowWhitespaceControls: boolean,
): boolean {
  for (const character of value) {
    const codePoint = character.codePointAt(0) ?? 0;
    if (
      codePoint === 0x7f ||
      (codePoint < 0x20 &&
        (!allowWhitespaceControls || !"\n\r\t".includes(character)))
    ) {
      return true;
    }
  }
  return false;
}

/** 构造新建或编辑 Subagent 表单初始值（深拷贝工具白名单与可见性授权数组）。 */
export function subagentForm(profile?: SubagentProfile): SubagentFormValues {
  if (!profile) {
    return {
      slug: "",
      name: "",
      description: "",
      system_prompt: "",
      model_profile_id: "",
      allowed_tool_ids: [],
      enabled: true,
      max_llm_requests: "",
      timeout_seconds: "",
      visibility_scope: "company",
      owner_department_id: "",
      visibility_grants: [],
    };
  }
  const ownerDeptId = profile.owner_department_id ?? "";
  return {
    slug: profile.slug,
    name: profile.name,
    description: profile.description,
    system_prompt: profile.system_prompt,
    model_profile_id: profile.model_profile_id ?? "",
    allowed_tool_ids: [...profile.allowed_tool_ids],
    enabled: profile.enabled,
    max_llm_requests:
      profile.max_llm_requests === null ? "" : String(profile.max_llm_requests),
    timeout_seconds:
      profile.timeout_seconds === null ? "" : String(profile.timeout_seconds),
    visibility_scope: profile.visibility_scope,
    owner_department_id: ownerDeptId,
    visibility_grants:
      profile.visibility_scope === "department" && ownerDeptId
        ? normalizeVisibilityGrants(ownerDeptId, profile.visibility_grants, true)
        : [],
  };
}

/** 校验 Subagent 表单并按后端 Python str.strip 规则规范化输出载荷。 */
export function validateSubagentForm(values: SubagentFormValues): {
  errors: FieldErrors<SubagentField>;
  createInput: SubagentCreateInput | null;
} {
  const errors: FieldErrors<SubagentField> = {};
  const slug = stripSubagentWhitespace(values.slug);
  if (!SUBAGENT_SLUG_PATTERN.test(slug)) {
    errors.slug =
      "Slug 需为 3–64 位小写字母、数字、连字符或下划线，且以字母或数字开头和结尾。";
  } else if (RESERVED_SUBAGENT_SLUGS.has(slug)) {
    errors.slug = `「${slug}」为系统保留字，请更换其他 Slug。`;
  }

  const name = stripSubagentWhitespace(values.name);
  if (
    !name ||
    unicodeLength(name) > MAX_SUBAGENT_NAME_LENGTH ||
    hasDisallowedSubagentControlCharacter(name, false)
  ) {
    errors.name =
      `名称需为 1–${MAX_SUBAGENT_NAME_LENGTH} 个 Unicode 字符，` +
      "且不能包含 C0 控制字符（U+0000–U+001F）或 DEL（U+007F）。";
  }

  const description = stripSubagentWhitespace(values.description);
  if (
    !description ||
    unicodeLength(description) > MAX_SUBAGENT_DESCRIPTION_LENGTH ||
    hasDisallowedSubagentControlCharacter(description, true)
  ) {
    errors.description =
      `描述需为 1–${MAX_SUBAGENT_DESCRIPTION_LENGTH.toLocaleString()} 个 Unicode 字符，` +
      "除换行、回车和制表符外不能包含其他 C0 控制字符或 DEL（U+007F）；" +
      "C1 控制字符（U+0080–U+009F）按后端规则允许。";
  }

  const systemPrompt = stripSubagentWhitespace(values.system_prompt);
  if (
    !systemPrompt ||
    unicodeLength(systemPrompt) > MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH ||
    hasDisallowedSubagentControlCharacter(systemPrompt, true)
  ) {
    errors.system_prompt =
      `系统提示词需为 1–${MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH.toLocaleString()} 个 Unicode 字符，` +
      "除换行、回车和制表符外不能包含其他 C0 控制字符或 DEL（U+007F）；" +
      "C1 控制字符（U+0080–U+009F）按后端规则允许。";
  }

  const allowedToolIds = Array.from(
    new Set(
      values.allowed_tool_ids.map((toolId) =>
        stripSubagentWhitespace(toolId),
      ),
    ),
  );
  if (allowedToolIds.length > MAX_SUBAGENT_ALLOWED_TOOLS) {
    errors.allowed_tool_ids =
      `工具白名单最多只能选择 ${MAX_SUBAGENT_ALLOWED_TOOLS} 项，` +
      `当前已选择 ${allowedToolIds.length} 项。`;
  }

  let maxLlmRequests: number | null = null;
  if (values.max_llm_requests.trim()) {
    const parsed = parseInteger(values.max_llm_requests);
    if (parsed === null || parsed < 1 || parsed > 50) {
      errors.max_llm_requests = "最大 LLM 请求次数需为 1 到 50 之间的整数。";
    } else {
      maxLlmRequests = parsed;
    }
  }

  let timeoutSeconds: number | null = null;
  if (values.timeout_seconds.trim()) {
    const parsed = parseInteger(values.timeout_seconds);
    if (parsed === null || parsed < 10 || parsed > 1800) {
      errors.timeout_seconds = "超时时间需为 10 到 1800 秒之间的整数。";
    } else {
      timeoutSeconds = parsed;
    }
  }

  const trimmedOwnerDeptId = values.owner_department_id.trim();
  if (values.visibility_scope === "department" && !trimmedOwnerDeptId) {
    errors.owner_department_id = "请选择部门共享子智能体的归属部门。";
  }

  if (Object.keys(errors).length > 0) {
    return { errors, createInput: null };
  }

  const visibilityPayload =
    values.visibility_scope === "department"
      ? {
          visibility_scope: "department" as const,
          owner_department_id: trimmedOwnerDeptId,
          visibility_grants: normalizeVisibilityGrants(
            trimmedOwnerDeptId,
            values.visibility_grants,
            true,
          ),
        }
      : {
          visibility_scope: "company" as const,
          owner_department_id: null,
          visibility_grants: [] as VisibilityGrantInput[],
        };

  return {
    errors,
    createInput: {
      slug,
      name,
      description,
      system_prompt: systemPrompt,
      model_profile_id:
        stripSubagentWhitespace(values.model_profile_id) || null,
      allowed_tool_ids: allowedToolIds,
      enabled: values.enabled,
      max_llm_requests: maxLlmRequests,
      timeout_seconds: timeoutSeconds,
      ...visibilityPayload,
    },
  };
}
