import type { ApiSchemas } from "../../shared/api/client";
import {
  CONTROL_CHARACTER_PATTERN,
  DEFAULT_HTTPS_URL_MAX_LENGTH,
  parseInteger,
  parseRatio,
  unicodeLength,
  validateHttpsUrl,
  type FieldErrors,
} from "./modelShared";

export const MODEL_PROFILE_NAME_MAX_LENGTH = 120;
export const MODEL_BASE_URL_MAX_LENGTH = DEFAULT_HTTPS_URL_MAX_LENGTH;
export const MODEL_NAME_MAX_LENGTH = 200;
export const API_KEY_MAX_LENGTH = 16_384;
export const CONTEXT_WINDOW_TOKEN_LIMIT_MAX = 1_048_576;
export const OUTPUT_TOKEN_LIMIT_MAX = 1_000_000;
export const TIMEOUT_SECONDS_MAX = 3_600;
export const MAX_CONCURRENCY = 128;

// 直连 OpenAPI 管理端模型配置与上下文压缩策略契约。
export type ModelProfile = ApiSchemas["ModelProfileResponse"];
export type TokenizerEncoding = ModelProfile["tokenizer_encoding"];
export const TOKENIZER_ENCODINGS: readonly TokenizerEncoding[] = [
  "o200k_base",
  "cl100k_base",
] as const;

export type ModelProfileListResponse = ApiSchemas["ModelProfileListResponse"];
export type ModelProfileCreateInput = ApiSchemas["ModelProfileCreateRequest"];
export type CompressionPolicy = ApiSchemas["CompressionPolicyResponse"];
export type CompressionPolicyInput = ApiSchemas["CompressionPolicyRequest"];

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

/** 校验模型配置表单，合法时组装创建入参。 */
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
  const urlError = validateHttpsUrl(baseUrl, MODEL_BASE_URL_MAX_LENGTH);
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

/** 校验上下文压缩策略表单，确保所选模型已启用且比例之和小于 1。 */
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

function deriveTokenBudget(
  triggerTokens: number | null,
  ratio: number | null,
  tokenCap: number | null,
): number | null {
  return triggerTokens === null || ratio === null || tokenCap === null
    ? null
    : Math.min(triggerTokens * ratio, tokenCap);
}

/** 计算压缩策略在当前触发阈值与上限约束下的实际 Token 预算。 */
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

/** 从压缩策略表单字符串值实时推导 Token 预算预览。 */
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

/** 生成新建模型配置表单的默认初始值。 */
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

/** 将服务端压缩策略对象映射为可编辑表单初始值。 */
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

/** 仅同时启用且支持流式与工具调用的模型才可设为默认对话模型。 */
export function modelCanBeDefault(profile: ModelProfile): boolean {
  return (
    profile.enabled &&
    profile.supports_stream &&
    profile.supports_tools
  );
}
