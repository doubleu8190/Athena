import type { AdminPageQuery, SystemPromptKind } from "./model";
import { ADMIN_QUERY_ROOT } from "../../app/queryClient";

export const adminKeys = {
  all: ADMIN_QUERY_ROOT,
  compressionPolicy: () =>
    ["admin", "llm", "compression-policy"] as const,
  mcpServer: (serverId: string) =>
    ["admin", "mcp", "servers", "detail", serverId] as const,
  mcpServers: (query: AdminPageQuery) =>
    [
      "admin",
      "mcp",
      "servers",
      "list",
      query.page,
      query.pageSize,
    ] as const,
  mcpServersAll: () => ["admin", "mcp", "servers"] as const,
  modelProfiles: () =>
    ["admin", "llm", "model-profiles"] as const,
  systemPrompt: (kind: SystemPromptKind) =>
    ["admin", "system-prompts", kind, "current"] as const,
  systemPromptVersion: (
    kind: SystemPromptKind,
    versionId: string,
  ) =>
    [
      "admin",
      "system-prompts",
      kind,
      "versions",
      "detail",
      versionId,
    ] as const,
  systemPromptVersions: (
    kind: SystemPromptKind,
    query: AdminPageQuery,
  ) =>
    [
      "admin",
      "system-prompts",
      kind,
      "versions",
      "list",
      query.page,
      query.pageSize,
    ] as const,
  systemPromptVersionsAll: (kind: SystemPromptKind) =>
    [
      "admin",
      "system-prompts",
      kind,
      "versions",
    ] as const,
  tools: (query: AdminPageQuery) =>
    [
      "admin",
      "tools",
      "list",
      query.page,
      query.pageSize,
    ] as const,
  toolsAll: () => ["admin", "tools"] as const,
};

