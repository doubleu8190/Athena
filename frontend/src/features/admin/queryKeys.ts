import type { AdminPageQuery } from "./model";
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

