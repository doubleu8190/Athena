import {
  normalizeSessionSearchQuery,
  type SessionListQuery,
} from "./model";

export const sessionKeys = {
  all: ["sessions"] as const,
  detail: (sessionId: string) =>
    ["sessions", "detail", sessionId] as const,
  details: () => ["sessions", "detail"] as const,
  // 将归一化后的搜索词纳入缓存键，确保不同搜索结果与默认历史列表完全隔离。
  list: (query: SessionListQuery = {}) =>
    [
      "sessions",
      "list",
      query.page ?? 1,
      query.pageSize ?? 20,
      query.projectId ?? null,
      Boolean(query.unbound),
      normalizeSessionSearchQuery(query.q) || null,
    ] as const,
  lists: () => ["sessions", "list"] as const,
  recent: () => ["sessions", "list", 1, 8, null, false, null] as const,
};

