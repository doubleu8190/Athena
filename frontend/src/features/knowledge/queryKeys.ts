import type {
  KnowledgeDocumentListQuery,
  KnowledgeListQuery,
} from "./model";

export const knowledgeKeys = {
  all: ["knowledge"] as const,
  detail: (baseId: string) =>
    ["knowledge", "detail", baseId] as const,
  directory: () => ["knowledge", "directory"] as const,
  documentList: (
    baseId: string,
    query: KnowledgeDocumentListQuery = {},
  ) =>
    [
      "knowledge",
      "documents",
      baseId,
      query.page ?? 1,
      query.pageSize ?? 10,
    ] as const,
  documents: (baseId: string) =>
    ["knowledge", "documents", baseId] as const,
  list: (query: KnowledgeListQuery = {}) =>
    [
      "knowledge",
      "list",
      query.page ?? 1,
      query.pageSize ?? 20,
      query.visibilityScope ?? "all",
      query.ownerDepartmentId ?? "",
    ] as const,
  lists: () => ["knowledge", "list"] as const,
  project: (projectId: string) =>
    ["knowledge", "project", projectId] as const,
  versions: (baseId: string, documentId: string) =>
    ["knowledge", "versions", baseId, documentId] as const,
};
