export const MODEL_PROFILE_NAME_MAX_LENGTH = 120;
export const MODEL_BASE_URL_MAX_LENGTH = 2_048;
export const MODEL_NAME_MAX_LENGTH = 200;
export const CREDENTIAL_ENV_MAX_LENGTH = 128;
export const TOKEN_LIMIT_MAX = 1_000_000;
export const TIMEOUT_SECONDS_MAX = 3_600;
export const MAX_CONCURRENCY = 128;
export const MCP_SERVER_NAME_MAX_LENGTH = 120;
export const MCP_SECRET_MAX_BYTES = 16 * 1024;

export const TOKENIZER_ENCODINGS = [
  "o200k_base",
  "cl100k_base",
] as const;

export type TokenizerEncoding = (typeof TOKENIZER_ENCODINGS)[number];
export type McpTransport = "streamable_http" | "stdio";

export interface ModelProfile {
  id: string;
  name: string;
  protocol: string;
  base_url: string;
  model: string;
  credential_env: string;
  context_window_tokens: number;
  tokenizer_encoding: TokenizerEncoding;
  supports_stream: boolean;
  supports_tools: boolean;
  supports_parallel_tool_calls: boolean;
  max_output_tokens: number;
  request_timeout_seconds: number;
  execution_timeout_seconds: number;
  max_concurrency: number;
  temperature: number | null;
  enabled: boolean;
  created_at: string;
}

export interface ModelProfileListResponse {
  items: ModelProfile[];
  default_profile_id: string | null;
}

export interface ModelProfileCreateInput {
  name: string;
  base_url: string;
  model: string;
  credential_env: string;
  context_window_tokens: number;
  tokenizer_encoding: TokenizerEncoding;
  supports_stream: boolean;
  supports_tools: boolean;
  supports_parallel_tool_calls: boolean;
  max_output_tokens: number;
  request_timeout_seconds: number;
  execution_timeout_seconds: number;
  max_concurrency: number;
  temperature: number | null;
}

export interface CompressionPolicy {
  id: string | null;
  compression_model_profile_id: string | null;
  trigger_ratio: number | null;
  recent_ratio: number | null;
  summary_ratio: number | null;
  prompt_template_version: string | null;
  created_at: string | null;
}

export interface CompressionPolicyInput {
  compression_model_profile_id: string;
  trigger_ratio: number;
  recent_ratio: number;
  summary_ratio: number;
}

export interface McpSecretMetadata {
  name: string;
  has_value: boolean;
}

export interface McpHttpConfig {
  url: string;
}

export interface McpStdioConfig {
  command: string;
  args: string[];
  cwd: string | null;
}

export interface McpServer {
  id: string;
  name: string;
  transport: McpTransport;
  config: McpHttpConfig | McpStdioConfig;
  secrets: McpSecretMetadata[];
  config_version: number;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  tools_discovered_at: string | null;
}

export interface McpServerListResponse {
  items: McpServer[];
  total: number;
  page: number;
  page_size: number;
}

export interface McpHttpWriteInput {
  name: string;
  transport: "streamable_http";
  url: string;
  secrets: Record<string, string>;
}

export interface McpStdioWriteInput {
  name: string;
  transport: "stdio";
  command: string;
  args: string[];
  cwd: string | null;
  secrets: Record<string, string>;
}

export type McpServerWriteInput = McpHttpWriteInput | McpStdioWriteInput;

export interface McpServerUpdateInput {
  expected_config_version: number;
  server: McpServerWriteInput;
}

export interface AdminTool {
  tool_id: string;
  model_name: string;
  description: string;
  input_schema: Record<string, unknown>;
  server_id: string;
  remote_name: string;
  requires_approval: boolean;
}

export interface AdminToolListResponse {
  items: AdminTool[];
  total: number;
  page: number;
  page_size: number;
}

export interface AdminPageQuery {
  page: number;
  pageSize: number;
}

export interface ModelProfileFormValues {
  name: string;
  base_url: string;
  model: string;
  credential_env: string;
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
  trigger_ratio: string;
  recent_ratio: string;
  summary_ratio: string;
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
  return Array.from(value).length;
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
  const credentialEnv = values.credential_env.trim();
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
  if (
    !credentialEnv ||
    credentialEnv.length > CREDENTIAL_ENV_MAX_LENGTH ||
    !ENV_NAME_PATTERN.test(credentialEnv) ||
    !credentialEnv.startsWith("ATHENA_LLM_CREDENTIAL_")
  ) {
    errors.credential_env =
      "必须是以 ATHENA_LLM_CREDENTIAL_ 开头的合法环境变量名。";
  }
  if (
    contextTokens === null ||
    contextTokens < 1 ||
    contextTokens > TOKEN_LIMIT_MAX
  ) {
    errors.context_window_tokens =
      "上下文窗口需为 1–1,000,000 的整数。";
  }
  if (
    maxOutputTokens === null ||
    maxOutputTokens < 1 ||
    maxOutputTokens > TOKEN_LIMIT_MAX ||
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
      credential_env: credentialEnv,
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
  const trigger = parseRatio(values.trigger_ratio);
  const recent = parseRatio(values.recent_ratio);
  const summary = parseRatio(values.summary_ratio);
  const selected = enabledProfiles.find(
    (profile) =>
      profile.id === values.compression_model_profile_id &&
      profile.enabled,
  );

  if (!selected) {
    errors.compression_model_profile_id =
      "请选择一个当前已启用的模型版本。";
  }
  if (trigger === null || trigger <= 0 || trigger >= 1) {
    errors.trigger_ratio = "触发比例必须大于 0 且小于 1。";
  }
  if (recent === null || recent <= 0 || recent >= 1) {
    errors.recent_ratio = "近期原文比例必须大于 0 且小于 1。";
  }
  if (summary === null || summary <= 0 || summary >= 1) {
    errors.summary_ratio = "摘要比例必须大于 0 且小于 1。";
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
      trigger_ratio: trigger as number,
      recent_ratio: recent as number,
      summary_ratio: summary as number,
    },
  };
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
    credential_env: "ATHENA_LLM_CREDENTIAL_",
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
    trigger_ratio:
      policy.trigger_ratio === null
        ? "0.8"
        : String(policy.trigger_ratio),
    recent_ratio:
      policy.recent_ratio === null
        ? "0.2"
        : String(policy.recent_ratio),
    summary_ratio:
      policy.summary_ratio === null
        ? "0.1"
        : String(policy.summary_ratio),
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
  const httpConfig =
    server.transport === "streamable_http"
      ? (server.config as McpHttpConfig)
      : null;
  const stdioConfig =
    server.transport === "stdio"
      ? (server.config as McpStdioConfig)
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

