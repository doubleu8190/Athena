import type { ApiSchemas } from "../../shared/api/client";
import {
  CONTROL_CHARACTER_PATTERN,
  unicodeLength,
  validateHttpsUrl,
  type FieldErrors,
} from "./modelShared";

export const MCP_SERVER_NAME_MAX_LENGTH = 120;
export const MCP_SECRET_MAX_BYTES = 16 * 1024;

// 直连 OpenAPI MCP Server 与管理员工具目录契约。
export type McpServer = ApiSchemas["McpServerResponse"];
export type McpTransport = McpServer["transport"];
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

/** 校验 MCP Server 表单与秘密列表，按传输方式生成全量替换写入载荷。 */
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
