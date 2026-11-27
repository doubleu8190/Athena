import type { ApiSchemas } from "../../shared/api/client";
import { countCodePoints } from "../../shared/lib/text";

export const MODEL_PROFILE_NAME_MAX_LENGTH = 120;
export const MODEL_BASE_URL_MAX_LENGTH = 2_048;
export const MODEL_NAME_MAX_LENGTH = 200;
export const API_KEY_MAX_LENGTH = 16_384;
export const CONTEXT_WINDOW_TOKEN_LIMIT_MAX = 1_048_576;
export const OUTPUT_TOKEN_LIMIT_MAX = 1_000_000;
export const TIMEOUT_SECONDS_MAX = 3_600;
export const MAX_CONCURRENCY = 128;
export const MCP_SERVER_NAME_MAX_LENGTH = 120;
export const MCP_SECRET_MAX_BYTES = 16 * 1024;
export const SYSTEM_PROMPT_CONTENT_MAX_LENGTH = 100_000;

// 直连 OpenAPI 管理端模型配置、压缩策略、MCP、工具、系统提示词与组织部门契约。
export type ModelProfile = ApiSchemas["ModelProfileResponse"];
export type TokenizerEncoding = ModelProfile["tokenizer_encoding"];
export const TOKENIZER_ENCODINGS: readonly TokenizerEncoding[] = [
  "o200k_base",
  "cl100k_base",
] as const;

export type McpServer = ApiSchemas["McpServerResponse"];
export type McpTransport = McpServer["transport"];
export type SystemPromptKind = ApiSchemas["SystemPromptKind"];
export type SystemPromptVersion = ApiSchemas["SystemPromptVersionResponse"];
export type SystemPromptVersionSummary =
  ApiSchemas["SystemPromptVersionSummary"];
export type SystemPromptVersionListResponse =
  ApiSchemas["SystemPromptVersionListResponse"];
export type OrgDepartment = ApiSchemas["DepartmentResponse"];
export type OrgDepartmentListResponse = ApiSchemas["DepartmentListResponse"];
export type ModelProfileListResponse = ApiSchemas["ModelProfileListResponse"];
export type ModelProfileCreateInput = ApiSchemas["ModelProfileCreateRequest"];
export type CompressionPolicy = ApiSchemas["CompressionPolicyResponse"];
export type CompressionPolicyInput = ApiSchemas["CompressionPolicyRequest"];
export type McpSecretMetadata = ApiSchemas["McpSecretMetadataResponse"];
export type McpHttpConfig = ApiSchemas["McpHttpConfigResponse"];
export type McpStdioConfig = ApiSchemas["McpStdioConfigResponse"];
export type McpServerListResponse = ApiSchemas["McpServerListResponse"];
export type AdminTool = ApiSchemas["AdminToolResponse"];
export type AdminToolListResponse = ApiSchemas["AdminToolListResponse"];

// 判别联合写入类型：基于 UpdateRequest 去除乐观锁版本，确保 secrets 必填以同时满足注册与全量更新契约。
export type McpHttpWriteInput = Omit<
  ApiSchemas["HttpServerUpdateRequest"],
  "expected_config_version"
>;
export type McpStdioWriteInput = Omit<
  ApiSchemas["StdioServerUpdateRequest"],
  "expected_config_version"
>;
export type McpServerWriteInput = McpHttpWriteInput | McpStdioWriteInput;

export interface McpServerUpdateInput {
  expected_config_version: number;
  server: McpServerWriteInput;
}

export interface AdminPageQuery {
  page: number;
  pageSize: number;
}

export interface ModelProfileFormValues {
  name: string;
  base_url: string;
  model: string;
  api_key: string;
  context_window_tokens: string;
  tokenizer_encoding: TokenizerEncoding;
  supports_stream: boolean;
  supports_tools: boolean;
  supports_parallel_tool_calls: boolean;
  max_output_tokens: string;
  request_timeout_seconds: string;
  execution_timeout_seconds: string;
  max_concurrency: string;
  temperature: string;
}

export type ModelProfileField = keyof ModelProfileFormValues;
export type FieldErrors<Field extends string> = Partial<
  Record<Field, string>
>;

export interface CompressionPolicyFormValues {
  compression_model_profile_id: string;
  trigger_tokens: string;
  recent_ratio: string;
  recent_token_cap: string;
  summary_ratio: string;
  summary_token_cap: string;
}

export type CompressionPolicyField =
  keyof CompressionPolicyFormValues;

export interface McpSecretRow {
  id: string;
  name: string;
  value: string;
  existing: boolean;
}

export interface McpServerFormValues {
  name: string;
  transport: McpTransport;
  url: string;
  command: string;
  argsText: string;
  cwd: string;
  secrets: McpSecretRow[];
}

export type McpServerField =
  | "name"
  | "transport"
  | "url"
  | "command"
  | "argsText"
  | "cwd"
  | "secrets";

const ENV_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;
const HTTP_SECRET_NAME_PATTERN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
const CONTROL_CHARACTER_PATTERN = /\p{Cc}/u;
const RESERVED_HTTP_SECRET_NAMES = new Set([
  "host",
  "content-length",
  "connection",
  "transfer-encoding",
  "accept",
  "content-type",
  "mcp-protocol-version",
  "mcp-method",
  "mcp-name",
  "mcp-session-id",
  "last-event-id",
]);

function unicodeLength(value: string): number {
  return countCodePoints(value);
}

export function validateSystemPromptContent(content: string): string | null {
  if (!content.trim()) {
    return "系统提示词不能全部为空白。";
  }
  if (unicodeLength(content) > SYSTEM_PROMPT_CONTENT_MAX_LENGTH) {
    return `系统提示词不能超过 ${SYSTEM_PROMPT_CONTENT_MAX_LENGTH.toLocaleString()} 个字符。`;
  }
  return null;
}

function parseInteger(value: string): number | null {
  const normalized = value.trim();
  if (!/^\d+$/.test(normalized)) {
    return null;
  }
  const parsed = Number(normalized);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

function parseRatio(value: string): number | null {
  const normalized = value.trim();
  if (!normalized) {
    return null;
  }
  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}

export function validateHttpsUrl(
  value: string,
  maxLength = MODEL_BASE_URL_MAX_LENGTH,
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

export function validateModelProfileForm(
  values: ModelProfileFormValues,
): {
  errors: FieldErrors<ModelProfileField>;
  input: ModelProfileCreateInput | null;
} {
  const errors: FieldErrors<ModelProfileField> = {};
  const name = values.name.trim();
  const baseUrl = values.base_url.trim();
  const model = values.model.trim();
  const apiKey = values.api_key.trim();
  const contextTokens = parseInteger(values.context_window_tokens);
  const maxOutputTokens = parseInteger(values.max_output_tokens);
  const requestTimeout = parseInteger(
    values.request_timeout_seconds,
  );
  const executionTimeout = parseInteger(
    values.execution_timeout_seconds,
  );
  const maxConcurrency = parseInteger(values.max_concurrency);
  const temperature = values.temperature.trim()
    ? Number(values.temperature)
    : null;

  if (
    !name ||
    unicodeLength(name) > MODEL_PROFILE_NAME_MAX_LENGTH ||
    CONTROL_CHARACTER_PATTERN.test(name)
  ) {
    errors.name = "名称需为 1–120 个字符，且不能包含控制字符。";
  }
  const urlError = validateHttpsUrl(baseUrl);
  if (urlError) {
    errors.base_url = urlError;
  }
  if (
    !model ||
    unicodeLength(model) > MODEL_NAME_MAX_LENGTH ||
    CONTROL_CHARACTER_PATTERN.test(model)
  ) {
    errors.model = "模型标识需为 1–200 个字符。";
  }
  const apiKeyPayloadBytes = new TextEncoder().encode(
    JSON.stringify({ api_key: apiKey }),
  ).length;
  if (!apiKey || apiKey.includes("\0") || apiKeyPayloadBytes > API_KEY_MAX_LENGTH) {
    errors.api_key = "API Key 不能为空、不能包含 NUL 字符，且加密内容不能超过 16 KiB。";
  }
  if (
    contextTokens === null ||
    contextTokens < 1 ||
    contextTokens > CONTEXT_WINDOW_TOKEN_LIMIT_MAX
  ) {
    errors.context_window_tokens =
      "上下文窗口需为 1–1,048,576 的整数。";
  }
  if (
    maxOutputTokens === null ||
    maxOutputTokens < 1 ||
    maxOutputTokens > OUTPUT_TOKEN_LIMIT_MAX ||
    (contextTokens !== null && maxOutputTokens > contextTokens)
  ) {
    errors.max_output_tokens =
      "最大输出需为正整数且不能超过上下文窗口。";
  }
  if (
    values.supports_parallel_tool_calls &&
    !values.supports_tools
  ) {
    errors.supports_parallel_tool_calls =
      "启用并行工具调用前必须先启用工具支持。";
  }
  if (
    requestTimeout === null ||
    requestTimeout < 1 ||
    requestTimeout > TIMEOUT_SECONDS_MAX
  ) {
    errors.request_timeout_seconds = "请求超时需为 1–3,600 秒。";
  }
  if (
    executionTimeout === null ||
    executionTimeout < 1 ||
    executionTimeout > TIMEOUT_SECONDS_MAX
  ) {
    errors.execution_timeout_seconds = "执行超时需为 1–3,600 秒。";
  }
  if (
    maxConcurrency === null ||
    maxConcurrency < 1 ||
    maxConcurrency > MAX_CONCURRENCY
  ) {
    errors.max_concurrency = "最大并发需为 1–128。";
  }
  if (
    temperature !== null &&
    (!Number.isFinite(temperature) || temperature < 0 || temperature > 2)
  ) {
    errors.temperature = "temperature 需在 0–2 之间，或留空。";
  }

  if (Object.keys(errors).length > 0) {
    return { errors, input: null };
  }
  return {
    errors,
    input: {
      name,
      base_url: baseUrl,
      model,
      api_key: apiKey,
      context_window_tokens: contextTokens as number,
      tokenizer_encoding: values.tokenizer_encoding,
      supports_stream: values.supports_stream,
      supports_tools: values.supports_tools,
      supports_parallel_tool_calls:
        values.supports_tools &&
        values.supports_parallel_tool_calls,
      max_output_tokens: maxOutputTokens as number,
      request_timeout_seconds: requestTimeout as number,
      execution_timeout_seconds: executionTimeout as number,
      max_concurrency: maxConcurrency as number,
      temperature,
    },
  };
}

export function validateCompressionPolicyForm(
  values: CompressionPolicyFormValues,
  enabledProfiles: ModelProfile[],
): {
  errors: FieldErrors<CompressionPolicyField>;
  input: CompressionPolicyInput | null;
} {
  const errors: FieldErrors<CompressionPolicyField> = {};
  const triggerTokens = parseInteger(values.trigger_tokens);
  const recent = parseRatio(values.recent_ratio);
  const recentTokenCap = parseInteger(values.recent_token_cap);
  const summary = parseRatio(values.summary_ratio);
  const summaryTokenCap = parseInteger(values.summary_token_cap);
  const selected = enabledProfiles.find(
    (profile) =>
      profile.id === values.compression_model_profile_id &&
      profile.enabled,
  );

  if (!selected) {
    errors.compression_model_profile_id =
      "请选择一个当前已启用的模型版本。";
  }
  if (triggerTokens === null || triggerTokens <= 0) {
    errors.trigger_tokens = "触发 Token 必须为正整数。";
  }
  if (recent === null || recent <= 0 || recent >= 1) {
    errors.recent_ratio = "近期原文比例必须大于 0 且小于 1。";
  }
  if (recentTokenCap === null || recentTokenCap <= 0) {
    errors.recent_token_cap = "近期原文 Token 上限必须为正整数。";
  }
  if (summary === null || summary <= 0 || summary >= 1) {
    errors.summary_ratio = "摘要比例必须大于 0 且小于 1。";
  }
  if (summaryTokenCap === null || summaryTokenCap <= 0) {
    errors.summary_token_cap = "摘要 Token 上限必须为正整数。";
  }
  if (
    recent !== null &&
    summary !== null &&
    recent + summary >= 1
  ) {
    errors.summary_ratio = "近期原文与摘要比例之和必须小于 1。";
  }

  if (Object.keys(errors).length > 0 || !selected) {
    return { errors, input: null };
  }
  return {
    errors,
    input: {
      compression_model_profile_id: selected.id,
      trigger_tokens: triggerTokens as number,
      recent_ratio: recent as number,
      recent_token_cap: recentTokenCap as number,
      summary_ratio: summary as number,
      summary_token_cap: summaryTokenCap as number,
    },
  };
}

interface CompressionBudgetInput {
  trigger_tokens: number | null;
  recent_ratio: number | null;
  recent_token_cap: number | null;
  summary_ratio: number | null;
  summary_token_cap: number | null;
}

export interface CompressionPolicyBudgets {
  recent: number | null;
  summary: number | null;
}

function deriveTokenBudget(
  triggerTokens: number | null,
  ratio: number | null,
  tokenCap: number | null,
): number | null {
  return triggerTokens === null || ratio === null || tokenCap === null
    ? null
    : Math.min(triggerTokens * ratio, tokenCap);
}

export function compressionPolicyBudgets(
  policy: CompressionBudgetInput,
): CompressionPolicyBudgets {
  return {
    recent: deriveTokenBudget(
      policy.trigger_tokens,
      policy.recent_ratio,
      policy.recent_token_cap,
    ),
    summary: deriveTokenBudget(
      policy.trigger_tokens,
      policy.summary_ratio,
      policy.summary_token_cap,
    ),
  };
}

export function compressionPolicyFormBudgets(
  values: CompressionPolicyFormValues,
): CompressionPolicyBudgets {
  return compressionPolicyBudgets({
    trigger_tokens: parseInteger(values.trigger_tokens),
    recent_ratio: parseRatio(values.recent_ratio),
    recent_token_cap: parseInteger(values.recent_token_cap),
    summary_ratio: parseRatio(values.summary_ratio),
    summary_token_cap: parseInteger(values.summary_token_cap),
  });
}

function validateSecretName(
  name: string,
  transport: McpTransport,
): string | null {
  if (!name) {
    return "秘密名称不能为空。";
  }
  if (name.includes("\x00")) {
    return "秘密名称不能包含 NUL 字符。";
  }
  if (transport === "streamable_http") {
    const normalized = name.toLowerCase();
    if (
      !HTTP_SECRET_NAME_PATTERN.test(name) ||
      RESERVED_HTTP_SECRET_NAMES.has(normalized) ||
      normalized.startsWith("mcp-param-")
    ) {
      return "HTTP 秘密名称必须是合法 header token，且不能使用协议保留名。";
    }
    return null;
  }
  return ENV_NAME_PATTERN.test(name)
    ? null
    : "stdio 秘密名称必须是合法 POSIX 环境变量名。";
}

export function validateMcpServerForm(values: McpServerFormValues): {
  errors: FieldErrors<McpServerField>;
  input: McpServerWriteInput | null;
} {
  const errors: FieldErrors<McpServerField> = {};
  const name = values.name.trim();
  if (
    !name ||
    unicodeLength(name) > MCP_SERVER_NAME_MAX_LENGTH ||
    CONTROL_CHARACTER_PATTERN.test(name)
  ) {
    errors.name = "Server 名称需为 1–120 个字符，且不能包含控制字符。";
  }

  const secrets: Record<string, string> = {};
  const normalizedNames = new Set<string>();
  let secretBytes = 0;
  values.secrets.forEach((row) => {
    const secretName = row.name.trim();
    const completelyEmpty = !secretName && !row.value;
    if (completelyEmpty && !row.existing) {
      return;
    }
    if (!secretName || (!row.value && row.existing)) {
      errors.secrets =
        "已有秘密必须重新输入值，或显式移除该行。";
      return;
    }
    if (!row.value) {
      errors.secrets = "秘密值不能为空。";
      return;
    }
    const nameError = validateSecretName(
      secretName,
      values.transport,
    );
    if (nameError) {
      errors.secrets = nameError;
      return;
    }
    const duplicateKey =
      values.transport === "streamable_http"
        ? secretName.toLowerCase()
        : secretName;
    if (normalizedNames.has(duplicateKey)) {
      errors.secrets = "秘密名称不能重复。";
      return;
    }
    if (
      row.value.includes("\x00") ||
      (values.transport === "streamable_http" &&
        (row.value.includes("\r") || row.value.includes("\n")))
    ) {
      errors.secrets = "秘密值包含不允许的控制字符。";
      return;
    }
    normalizedNames.add(duplicateKey);
    secretBytes += new TextEncoder().encode(
      secretName + row.value,
    ).length;
    secrets[secretName] = row.value;
  });
  if (secretBytes > MCP_SECRET_MAX_BYTES) {
    errors.secrets = "秘密名称和值的 UTF-8 总大小不能超过 16 KiB。";
  }

  if (values.transport === "streamable_http") {
    const urlError = validateHttpsUrl(values.url);
    if (urlError) {
      errors.url = urlError;
    }
    if (Object.keys(errors).length > 0) {
      return { errors, input: null };
    }
    return {
      errors,
      input: {
        name,
        transport: "streamable_http",
        url: values.url.trim(),
        secrets,
      },
    };
  }

  const command = values.command.trim();
  if (
    !command.startsWith("/") ||
    command.includes("\x00")
  ) {
    errors.command = "stdio command 必须是绝对路径，且不能包含 NUL。";
  }
  const cwd = values.cwd.trim();
  if (cwd && (!cwd.startsWith("/") || cwd.includes("\x00"))) {
    errors.cwd = "工作目录必须是绝对路径，且不能包含 NUL。";
  }
  const args = values.argsText
    .split("\n")
    .map((line) => line.replace(/\r$/, ""))
    .filter((line) => line.length > 0);
  if (args.some((argument) => argument.includes("\x00"))) {
    errors.argsText = "参数不能包含 NUL 字符。";
  }
  if (Object.keys(errors).length > 0) {
    return { errors, input: null };
  }
  return {
    errors,
    input: {
      name,
      transport: "stdio",
      command,
      args,
      cwd: cwd || null,
      secrets,
    },
  };
}

export function defaultModelProfileForm(): ModelProfileFormValues {
  return {
    name: "",
    base_url: "",
    model: "",
    api_key: "",
    context_window_tokens: "128000",
    tokenizer_encoding: "o200k_base",
    supports_stream: true,
    supports_tools: true,
    supports_parallel_tool_calls: false,
    max_output_tokens: "8192",
    request_timeout_seconds: "120",
    execution_timeout_seconds: "300",
    max_concurrency: "4",
    temperature: "",
  };
}

export function compressionPolicyForm(
  policy: CompressionPolicy,
): CompressionPolicyFormValues {
  return {
    compression_model_profile_id:
      policy.compression_model_profile_id ?? "",
    trigger_tokens:
      policy.trigger_tokens === null
        ? "262144"
        : String(policy.trigger_tokens),
    recent_ratio:
      policy.recent_ratio === null
        ? "0.2"
        : String(policy.recent_ratio),
    recent_token_cap:
      policy.recent_token_cap === null
        ? "32768"
        : String(policy.recent_token_cap),
    summary_ratio:
      policy.summary_ratio === null
        ? "0.1"
        : String(policy.summary_ratio),
    summary_token_cap:
      policy.summary_token_cap === null
        ? "8192"
        : String(policy.summary_token_cap),
  };
}

export function mcpServerForm(server?: McpServer): McpServerFormValues {
  if (!server) {
    return {
      name: "",
      transport: "streamable_http",
      url: "",
      command: "",
      argsText: "",
      cwd: "",
      secrets: [],
    };
  }
  // 按传输方式与结构键收窄联合配置，避免不安全类型断言。
  const httpConfig =
    server.transport === "streamable_http" && "url" in server.config
      ? server.config
      : null;
  const stdioConfig =
    server.transport === "stdio" && "command" in server.config
      ? server.config
      : null;
  return {
    name: server.name,
    transport: server.transport,
    url: httpConfig?.url ?? "",
    command: stdioConfig?.command ?? "",
    argsText: stdioConfig?.args.join("\n") ?? "",
    cwd: stdioConfig?.cwd ?? "",
    secrets: server.secrets.map((secret, index) => ({
      id: `existing-${index}-${secret.name}`,
      name: secret.name,
      value: "",
      existing: true,
    })),
  };
}

export function modelCanBeDefault(profile: ModelProfile): boolean {
  return (
    profile.enabled &&
    profile.supports_stream &&
    profile.supports_tools
  );
}

// ---------------- Subagent 管理模型与表单校验 ----------------

// 直连 OpenAPI Subagent 管理与可用工具目录契约。
export type SubagentProfile = ApiSchemas["SubagentResponse"];
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
}

export type SubagentField =
  | "slug"
  | "name"
  | "description"
  | "system_prompt"
  | "model_profile_id"
  | "allowed_tool_ids"
  | "max_llm_requests"
  | "timeout_seconds";

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
    };
  }
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
  };
}

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

  if (Object.keys(errors).length > 0) {
    return { errors, createInput: null };
  }

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
    },
  };
}

