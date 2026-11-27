import type { ApiSchemas } from "../../shared/api/client";
import {
  normalizeVisibilityGrants,
  visibilityGrantsEqual,
} from "../access/visibilityGrants";
import type { VisibilityGrantInput } from "../knowledge/model";
import {
  CONTROL_CHARACTER_PATTERN,
  unicodeLength,
  validateHttpsUrl,
  type FieldErrors,
} from "./modelShared";

export const MCP_SERVER_NAME_MAX_LENGTH = 120;
export const MCP_SECRET_MAX_BYTES = 16 * 1024;
export const TOOL_DISPLAY_NAME_MAX_LENGTH = 120;
export const APPROVAL_SUMMARY_TEMPLATE_MAX_LENGTH = 500;
export const TOOL_PARAMETER_LABELS_MAX_COUNT = 50;
export const TOOL_PARAMETER_LABEL_KEY_MAX_LENGTH = 128;
export const TOOL_PARAMETER_LABEL_VALUE_MAX_LENGTH = 120;

// 直连 OpenAPI MCP Server 与管理员工具目录契约。
export type McpServer = ApiSchemas["McpServerResponse"];
export type McpTransport = McpServer["transport"];
export type McpVisibilityScope = McpServer["visibility_scope"];
export type McpSecretMetadata = ApiSchemas["McpSecretMetadataResponse"];
export type McpHttpConfig = ApiSchemas["McpHttpConfigResponse"];
export type McpStdioConfig = ApiSchemas["McpStdioConfigResponse"];
export type McpServerListResponse = ApiSchemas["McpServerListResponse"];
export type AdminTool = ApiSchemas["AdminToolResponse"];
export type AdminToolListResponse = ApiSchemas["AdminToolListResponse"];
export type AdminToolUpdateInput = ApiSchemas["ToolApprovalPolicyRequest"];
export type AdminToolBatchApprovalInput =
  ApiSchemas["ToolBatchApprovalRequest"];
export type AdminToolBatchApprovalResponse =
  ApiSchemas["AdminToolBatchApprovalResponse"];

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

export interface McpServerPatchInput {
  expected_config_version: number;
  name?: string | null;
  config?: Record<string, unknown> | null;
  secrets_upsert?: Record<string, string> | null;
  secrets_remove?: string[] | null;
  visibility_scope?: McpVisibilityScope | null;
  owner_department_id?: string | null;
  visibility_grants?: ApiSchemas["VisibilityGrantRequest"][] | null;
  enabled?: boolean | null;
  rediscover?: boolean;
}

export interface McpServerEnabledInput {
  enabled: boolean;
  expected_config_version?: number | null;
}

export interface McpSecretRow {
  id: string;
  name: string;
  value: string;
  existing: boolean;
  hasValue?: boolean;
  originalName?: string;
}

export interface McpServerFormValues {
  name: string;
  transport: McpTransport;
  url: string;
  command: string;
  argsText: string;
  cwd: string;
  secrets: McpSecretRow[];
  visibility_scope: McpVisibilityScope;
  owner_department_id: string;
  visibility_grants: VisibilityGrantInput[];
  rediscover: boolean;
}

export type McpServerField =
  | "name"
  | "transport"
  | "url"
  | "command"
  | "argsText"
  | "cwd"
  | "secrets"
  | "owner_department_id";

const ENV_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;
const HTTP_SECRET_NAME_PATTERN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
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

function secretDedupeKey(name: string, transport: McpTransport): string {
  return transport === "streamable_http" ? name.toLowerCase() : name;
}

function validateBasicMcpFields(
  values: McpServerFormValues,
  errors: FieldErrors<McpServerField>,
): {
  name: string;
  trimmedUrl: string;
  command: string;
  cwd: string;
  args: string[];
  trimmedOwnerDeptId: string;
} {
  const name = values.name.trim();
  if (
    !name ||
    unicodeLength(name) > MCP_SERVER_NAME_MAX_LENGTH ||
    CONTROL_CHARACTER_PATTERN.test(name)
  ) {
    errors.name = "Server 名称需为 1–120 个字符，且不能包含控制字符。";
  }

  const trimmedOwnerDeptId = values.owner_department_id.trim();
  if (values.visibility_scope === "department" && !trimmedOwnerDeptId) {
    errors.owner_department_id = "请选择部门共享 MCP Server 的归属部门。";
  }

  const trimmedUrl = values.url.trim();
  const command = values.command.trim();
  const cwd = values.cwd.trim();
  const args = values.argsText
    .split("\n")
    .map((line) => line.replace(/\r$/, ""))
    .filter((line) => line.length > 0);

  if (values.transport === "streamable_http") {
    const urlError = validateHttpsUrl(values.url);
    if (urlError) {
      errors.url = urlError;
    }
  } else {
    if (!command.startsWith("/") || command.includes("\x00")) {
      errors.command = "stdio command 必须是绝对路径，且不能包含 NUL。";
    }
    if (cwd && (!cwd.startsWith("/") || cwd.includes("\x00"))) {
      errors.cwd = "工作目录必须是绝对路径，且不能包含 NUL。";
    }
    if (args.some((argument) => argument.includes("\x00"))) {
      errors.argsText = "参数不能包含 NUL 字符。";
    }
  }

  return {
    name,
    trimmedUrl,
    command,
    cwd,
    args,
    trimmedOwnerDeptId,
  };
}

function validateSecretValueCharacters(
  value: string,
  transport: McpTransport,
): boolean {
  if (value.includes("\x00")) {
    return false;
  }
  if (
    transport === "streamable_http" &&
    (value.includes("\r") || value.includes("\n"))
  ) {
    return false;
  }
  return true;
}

/** 校验 MCP Server 表单与秘密列表，按传输方式生成创建/全量替换写入载荷。 */
export function validateMcpServerForm(values: McpServerFormValues): {
  errors: FieldErrors<McpServerField>;
  input: McpServerWriteInput | null;
} {
  const errors: FieldErrors<McpServerField> = {};
  const { name, trimmedUrl, command, cwd, args, trimmedOwnerDeptId } =
    validateBasicMcpFields(values, errors);

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
      errors.secrets = "已有秘密必须重新输入值，或显式移除该行。";
      return;
    }
    if (!row.value) {
      errors.secrets = "秘密值不能为空。";
      return;
    }
    const nameError = validateSecretName(secretName, values.transport);
    if (nameError) {
      errors.secrets = nameError;
      return;
    }
    const duplicateKey = secretDedupeKey(secretName, values.transport);
    if (normalizedNames.has(duplicateKey)) {
      errors.secrets = "秘密名称不能重复。";
      return;
    }
    if (!validateSecretValueCharacters(row.value, values.transport)) {
      errors.secrets = "秘密值包含不允许的控制字符。";
      return;
    }
    normalizedNames.add(duplicateKey);
    secretBytes += new TextEncoder().encode(secretName + row.value).length;
    secrets[secretName] = row.value;
  });
  if (secretBytes > MCP_SECRET_MAX_BYTES) {
    errors.secrets = "秘密名称和值的 UTF-8 总大小不能超过 16 KiB。";
  }

  if (Object.keys(errors).length > 0) {
    return { errors, input: null };
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

  if (values.transport === "streamable_http") {
    return {
      errors,
      input: {
        name,
        transport: "streamable_http",
        url: trimmedUrl,
        secrets,
        ...visibilityPayload,
      },
    };
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
      ...visibilityPayload,
    },
  };
}

/** 校验编辑态 MCP Server 表单，组装保密局部更新载荷（留空既有凭据保留原密文）。 */
export function validateMcpServerPatchForm(
  values: McpServerFormValues,
  server: McpServer,
  expectedConfigVersion: number,
): {
  errors: FieldErrors<McpServerField>;
  input: McpServerPatchInput | null;
  hasChanges: boolean;
} {
  const errors: FieldErrors<McpServerField> = {};
  const { name, trimmedUrl, command, cwd, args, trimmedOwnerDeptId } =
    validateBasicMcpFields(values, errors);

  const secretsUpsert: Record<string, string> = {};
  const normalizedNames = new Set<string>();
  const keptExistingKeys = new Set<string>();
  const upsertDedupeKeys = new Set<string>();
  let secretBytes = 0;

  values.secrets.forEach((row) => {
    const secretName = (row.existing ? (row.originalName ?? row.name) : row.name).trim();
    const completelyEmpty = !secretName && !row.value;
    if (completelyEmpty && !row.existing) {
      return;
    }
    if (!secretName) {
      errors.secrets = "秘密名称不能为空。";
      return;
    }
    if (row.existing) {
      const dedupeKey = secretDedupeKey(secretName, values.transport);
      if (normalizedNames.has(dedupeKey)) {
        errors.secrets = "秘密名称不能重复。";
        return;
      }
      normalizedNames.add(dedupeKey);
      keptExistingKeys.add(dedupeKey);
      // 既有秘密留空表示保留原加密凭据；仅当输入非空新值时才纳入 secrets_upsert。
      if (row.value) {
        if (!validateSecretValueCharacters(row.value, values.transport)) {
          errors.secrets = "秘密值包含不允许的控制字符。";
          return;
        }
        secretBytes += new TextEncoder().encode(secretName + row.value).length;
        secretsUpsert[secretName] = row.value;
        upsertDedupeKeys.add(dedupeKey);
      }
      return;
    }

    if (!row.value) {
      errors.secrets = "新增秘密的值不能为空。";
      return;
    }
    const nameError = validateSecretName(secretName, values.transport);
    if (nameError) {
      errors.secrets = nameError;
      return;
    }
    const dedupeKey = secretDedupeKey(secretName, values.transport);
    if (normalizedNames.has(dedupeKey)) {
      errors.secrets = "秘密名称不能重复。";
      return;
    }
    if (!validateSecretValueCharacters(row.value, values.transport)) {
      errors.secrets = "秘密值包含不允许的控制字符。";
      return;
    }
    normalizedNames.add(dedupeKey);
    secretBytes += new TextEncoder().encode(secretName + row.value).length;
    secretsUpsert[secretName] = row.value;
    upsertDedupeKeys.add(dedupeKey);
  });

  if (secretBytes > MCP_SECRET_MAX_BYTES) {
    errors.secrets = "秘密名称和值的 UTF-8 总大小不能超过 16 KiB。";
  }

  if (Object.keys(errors).length > 0) {
    return { errors, input: null, hasChanges: false };
  }

  // 被用户删除且未被同名新增秘密覆盖的既有凭据才列入 secrets_remove，防止触发后端交集校验错误。
  const secretsRemove = server.secrets
    .map((secret) => secret.name)
    .filter((secretName) => {
      const key = secretDedupeKey(secretName, values.transport);
      return !keptExistingKeys.has(key) && !upsertDedupeKeys.has(key);
    });

  const patch: McpServerPatchInput = {
    expected_config_version: expectedConfigVersion,
  };
  let hasChanges = false;

  if (name !== server.name) {
    patch.name = name;
    hasChanges = true;
  }

  if (server.transport === "streamable_http" && "url" in server.config) {
    if (trimmedUrl !== server.config.url) {
      patch.config = { url: trimmedUrl };
      hasChanges = true;
    }
  } else if (server.transport === "stdio" && "command" in server.config) {
    const stdioConfig = server.config;
    const currentCwd = stdioConfig.cwd ?? "";
    const argsChanged =
      args.length !== stdioConfig.args.length ||
      args.some((arg, idx) => arg !== stdioConfig.args[idx]);
    if (
      command !== stdioConfig.command ||
      cwd !== currentCwd ||
      argsChanged
    ) {
      patch.config = {
        command,
        args,
        cwd: cwd || null,
      };
      hasChanges = true;
    }
  }

  if (Object.keys(secretsUpsert).length > 0) {
    patch.secrets_upsert = secretsUpsert;
    hasChanges = true;
  }
  if (secretsRemove.length > 0) {
    patch.secrets_remove = secretsRemove;
    hasChanges = true;
  }

  const initialGrants =
    server.visibility_scope === "department" && server.owner_department_id
      ? normalizeVisibilityGrants(
          server.owner_department_id,
          server.visibility_grants,
          true,
        )
      : [];
  const nextGrants =
    values.visibility_scope === "department"
      ? normalizeVisibilityGrants(
          trimmedOwnerDeptId,
          values.visibility_grants,
          true,
        )
      : [];
  const visibilityChanged =
    values.visibility_scope !== server.visibility_scope ||
    (values.visibility_scope === "department" &&
      (trimmedOwnerDeptId !== (server.owner_department_id ?? "") ||
        !visibilityGrantsEqual(nextGrants, initialGrants)));

  if (visibilityChanged) {
    hasChanges = true;
    if (values.visibility_scope === "company") {
      patch.visibility_scope = "company";
      patch.owner_department_id = null;
      patch.visibility_grants = [];
    } else {
      patch.visibility_scope = "department";
      patch.owner_department_id = trimmedOwnerDeptId;
      patch.visibility_grants = nextGrants;
    }
  }

  if (values.rediscover) {
    patch.rediscover = true;
    hasChanges = true;
  }

  return {
    errors,
    input: hasChanges ? patch : null,
    hasChanges,
  };
}

/** 构造新建或编辑 MCP Server 表单初始值（编辑态仅回显已有秘密名称，不回显密文）。 */
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
      visibility_scope: "company",
      owner_department_id: "",
      visibility_grants: [],
      rediscover: false,
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
  const ownerDeptId = server.owner_department_id ?? "";
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
      hasValue: secret.has_value,
      originalName: secret.name,
    })),
    visibility_scope: server.visibility_scope,
    owner_department_id: ownerDeptId,
    visibility_grants:
      server.visibility_scope === "department" && ownerDeptId
        ? normalizeVisibilityGrants(ownerDeptId, server.visibility_grants, true)
        : [],
    rediscover: false,
  };
}

// ---------------- 工具业务展示元数据表单校验 ----------------

export interface ToolParameterLabelRow {
  id: string;
  key: string;
  label: string;
}

export interface ToolMetadataFormValues {
  display_name: string;
  approval_summary_template: string;
  parameter_labels: ToolParameterLabelRow[];
}

export type ToolMetadataField =
  | "display_name"
  | "approval_summary_template"
  | "parameter_labels";

function hasDisallowedTemplateControlCharacter(value: string): boolean {
  for (const ch of value) {
    const cp = ch.codePointAt(0) ?? 0;
    if (
      cp === 0x7f ||
      (cp >= 0x80 && cp <= 0x9f) ||
      (cp < 0x20 && ch !== "\n" && ch !== "\r" && ch !== "\t")
    ) {
      return true;
    }
  }
  return false;
}

/** 从工具 JSON Schema properties 安全提取可选参数名列表，供模板占位符与标签快速带入。 */
export function extractToolSchemaParamNames(
  inputSchema: Record<string, unknown> | null | undefined,
): string[] {
  if (!inputSchema || typeof inputSchema !== "object") {
    return [];
  }
  const properties = inputSchema.properties;
  if (
    !properties ||
    typeof properties !== "object" ||
    Array.isArray(properties)
  ) {
    return [];
  }
  return Object.keys(properties as Record<string, unknown>);
}

/** 构造工具业务展示表单初始值。 */
export function toolMetadataForm(tool: AdminTool): ToolMetadataFormValues {
  return {
    display_name: tool.display_name ?? "",
    approval_summary_template: tool.approval_summary_template ?? "",
    parameter_labels: Object.entries(tool.parameter_labels ?? {}).map(
      ([key, label], index) => ({
        id: `param-${index}-${key}`,
        key,
        label,
      }),
    ),
  };
}

/** 校验工具业务展示元信息表单，留空字段规范化为 null 或 {}。 */
export function validateToolMetadataForm(values: ToolMetadataFormValues): {
  errors: FieldErrors<ToolMetadataField>;
  input: AdminToolUpdateInput | null;
} {
  const errors: FieldErrors<ToolMetadataField> = {};

  if (CONTROL_CHARACTER_PATTERN.test(values.display_name)) {
    errors.display_name = "中文展示名不能包含控制字符。";
  } else if (unicodeLength(values.display_name.trim()) > TOOL_DISPLAY_NAME_MAX_LENGTH) {
    errors.display_name = `中文展示名不能超过 ${TOOL_DISPLAY_NAME_MAX_LENGTH} 个字符。`;
  }

  if (hasDisallowedTemplateControlCharacter(values.approval_summary_template)) {
    errors.approval_summary_template =
      "审批摘要模板仅允许换行、回车与制表符，不能包含其他控制字符。";
  } else if (
    unicodeLength(values.approval_summary_template.trim()) >
    APPROVAL_SUMMARY_TEMPLATE_MAX_LENGTH
  ) {
    errors.approval_summary_template = `审批摘要模板不能超过 ${APPROVAL_SUMMARY_TEMPLATE_MAX_LENGTH} 个字符。`;
  }

  const parameterLabels: Record<string, string> = {};
  const nonEmptyRows = values.parameter_labels.filter(
    (row) => row.key.trim() || row.label.trim(),
  );
  if (nonEmptyRows.length > TOOL_PARAMETER_LABELS_MAX_COUNT) {
    errors.parameter_labels = `参数中文标签最多配置 ${TOOL_PARAMETER_LABELS_MAX_COUNT} 项。`;
  } else {
    for (const row of nonEmptyRows) {
      if (
        CONTROL_CHARACTER_PATTERN.test(row.key) ||
        CONTROL_CHARACTER_PATTERN.test(row.label)
      ) {
        errors.parameter_labels = "参数键名和中文标签不能包含控制字符。";
        break;
      }
      const key = row.key.trim();
      const label = row.label.trim();
      if (!key || !label) {
        errors.parameter_labels = "参数键名与中文标签均不能为空。";
        break;
      }
      if (
        unicodeLength(key) > TOOL_PARAMETER_LABEL_KEY_MAX_LENGTH ||
        unicodeLength(label) > TOOL_PARAMETER_LABEL_VALUE_MAX_LENGTH
      ) {
        errors.parameter_labels = `参数键名不超过 ${TOOL_PARAMETER_LABEL_KEY_MAX_LENGTH} 字符，中文标签不超过 ${TOOL_PARAMETER_LABEL_VALUE_MAX_LENGTH} 字符。`;
        break;
      }
      if (key in parameterLabels) {
        errors.parameter_labels = `参数键名「${key}」重复，请去重后再保存。`;
        break;
      }
      parameterLabels[key] = label;
    }
  }

  if (Object.keys(errors).length > 0) {
    return { errors, input: null };
  }

  return {
    errors,
    input: {
      display_name: values.display_name.trim() || null,
      approval_summary_template:
        values.approval_summary_template.trim() || null,
      parameter_labels: parameterLabels,
    },
  };
}
