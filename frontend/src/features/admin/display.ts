import { formatDateTime } from "../../shared/lib/format";
import type {
  McpHttpConfig,
  McpServer,
  McpStdioConfig,
  ModelProfile,
} from "./model";

export function modelCapabilities(profile: ModelProfile): string[] {
  const capabilities: string[] = [];
  if (profile.supports_stream) {
    capabilities.push("流式");
  }
  if (profile.supports_tools) {
    capabilities.push("工具");
  }
  if (
    profile.supports_tools &&
    profile.supports_parallel_tool_calls
  ) {
    capabilities.push("并行工具");
  }
  return capabilities;
}

export function formatRatio(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(2)}%`;
}

export function mcpConfigSummary(server: McpServer): string {
  if (server.transport === "streamable_http") {
    return (server.config as McpHttpConfig).url;
  }
  const config = server.config as McpStdioConfig;
  const commandLine = [config.command, ...config.args].join(" ");
  return config.cwd
    ? `${commandLine} · cwd=${config.cwd}`
    : commandLine;
}

export function mcpTransportLabel(server: McpServer): string {
  return server.transport === "streamable_http" ? "HTTPS" : "STDIO";
}

export function discoveredAtLabel(server: McpServer): string {
  return server.tools_discovered_at
    ? `发现于 ${formatDateTime(server.tools_discovered_at)}`
    : "后端尚无成功发现时间";
}

export function secretMetadataLabel(server: McpServer): string {
  if (server.secrets.length === 0) {
    return "无已保存秘密";
  }
  return server.secrets
    .map(
      (secret) =>
        `${secret.name}${secret.has_value ? "（已保存值）" : "（无值）"}`,
    )
    .join("、");
}

