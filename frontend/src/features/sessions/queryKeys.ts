import type { SessionListQuery } from "./model";

export const sessionKeys = {
  all: ["sessions"] as const,
  detail: (sessionId: string) =>
    ["sessions", "detail", sessionId] as const,
  details: () => ["sessions", "detail"] as const,
  list: (query: SessionListQuery = {}) =>
    [
      "sessions",
      "list",
      query.page ?? 1,
      query.pageSize ?? 20,
      query.projectId ?? null,
      Boolean(query.unbound),
    ] as const,
  lists: () => ["sessions", "list"] as const,
  recent: () => ["sessions", "list", 1, 8, null, false] as const,
};

