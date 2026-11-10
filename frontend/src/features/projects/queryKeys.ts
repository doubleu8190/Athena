import type { ProjectListQuery } from "./model";

export const projectKeys = {
  all: ["projects"] as const,
  detail: (projectId: string) =>
    ["projects", "detail", projectId] as const,
  directory: () => ["projects", "directory"] as const,
  list: (query: ProjectListQuery = {}) =>
    [
      "projects",
      "list",
      query.page ?? 1,
      query.pageSize ?? 20,
    ] as const,
  lists: () => ["projects", "list"] as const,
};

