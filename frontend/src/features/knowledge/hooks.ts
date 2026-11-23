import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";

import {
  bindProjectKnowledgeBase,
  createKnowledgeBase,
  deleteKnowledgeBase,
  deleteKnowledgeDocument,
  getKnowledgeBase,
  getKnowledgeChunk,
  importKnowledgeText,
  listAllKnowledgeBases,
  listKnowledgeBases,
  listKnowledgeDocuments,
  listKnowledgeVersions,
  listProjectKnowledgeBases,
  renameKnowledgeDocument,
  replaceKnowledgeFile,
  replaceKnowledgeText,
  retryKnowledgeVersion,
  searchKnowledgeBase,
  unbindProjectKnowledgeBase,
  updateKnowledgeBase,
  uploadKnowledgeDocument,
} from "./api";
import type {
  KnowledgeBaseInput,
  KnowledgeBasePatch,
  KnowledgeDocumentListQuery,
  KnowledgeListQuery,
} from "./model";
import { knowledgeKeys } from "./queryKeys";

async function invalidateKnowledgeCaches(
  queryClient: QueryClient,
  baseId?: string,
): Promise<void> {
  if (baseId) {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: knowledgeKeys.documents(baseId),
      }),
      queryClient.invalidateQueries({
        queryKey: knowledgeKeys.detail(baseId),
      }),
    ]);
  }
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: knowledgeKeys.lists() }),
    queryClient.invalidateQueries({ queryKey: knowledgeKeys.directory() }),
  ]);
}

async function invalidateDocumentCaches(
  queryClient: QueryClient,
  baseId: string,
  documentId: string,
): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({
      queryKey: knowledgeKeys.versions(baseId, documentId),
    }),
    invalidateKnowledgeCaches(queryClient, baseId),
  ]);
}

export function useKnowledgeDirectory(enabled = true) {
  return useQuery({
    queryKey: knowledgeKeys.directory(),
    queryFn: listAllKnowledgeBases,
    enabled,
  });
}

export function useKnowledgeList(query: KnowledgeListQuery = {}) {
  return useQuery({
    queryKey: knowledgeKeys.list(query),
    queryFn: () => listKnowledgeBases(query),
  });
}

export function useKnowledgeBase(
  baseId: string | undefined,
  enabled = true,
) {
  return useQuery({
    queryKey: knowledgeKeys.detail(baseId ?? ""),
    queryFn: () => getKnowledgeBase(baseId ?? ""),
    enabled: Boolean(baseId) && enabled,
  });
}

export function useKnowledgeDocuments(
  baseId: string,
  query: KnowledgeDocumentListQuery,
  enabled = true,
) {
  return useQuery({
    queryKey: knowledgeKeys.documentList(baseId, query),
    queryFn: () => listKnowledgeDocuments(baseId, query),
    enabled,
    refetchInterval: (queryState) =>
      queryState.state.data?.items.some(
        (document) =>
          document.pending_version_id !== null ||
          document.latest_version?.state === "queued" ||
          document.latest_version?.state === "processing",
      )
        ? 3_000
        : false,
  });
}

export function useKnowledgeVersions(
  baseId: string,
  documentId: string,
  enabled = true,
) {
  return useQuery({
    enabled,
    queryKey: knowledgeKeys.versions(baseId, documentId),
    queryFn: () => listKnowledgeVersions(baseId, documentId),
    refetchInterval: (queryState) => {
      const latest = queryState.state.data?.[0];
      return latest?.state === "queued" || latest?.state === "processing"
        ? 3_000
        : false;
    },
  });
}

export function useKnowledgeChunk(
  baseId: string,
  chunkId: string | null,
) {
  return useQuery({
    queryKey: ["knowledge", "chunk", baseId, chunkId ?? ""],
    queryFn: () => getKnowledgeChunk(baseId, chunkId ?? ""),
    enabled: Boolean(chunkId),
  });
}

export function useCreateKnowledgeBaseMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: KnowledgeBaseInput) => createKnowledgeBase(input),
    onSuccess: async () => {
      await invalidateKnowledgeCaches(queryClient);
    },
  });
}

export function useUpdateKnowledgeBaseMutation(baseId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: KnowledgeBasePatch) =>
      updateKnowledgeBase(baseId, input),
    onSuccess: async (base) => {
      queryClient.setQueryData(knowledgeKeys.detail(baseId), base);
      await invalidateKnowledgeCaches(queryClient, baseId);
    },
  });
}

export function useDeleteKnowledgeBaseMutation(baseId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => deleteKnowledgeBase(baseId),
    onSuccess: async () => {
      queryClient.removeQueries({
        queryKey: knowledgeKeys.detail(baseId),
      });
      queryClient.removeQueries({
        queryKey: knowledgeKeys.documents(baseId),
      });
      await invalidateKnowledgeCaches(queryClient);
    },
  });
}

export function useImportKnowledgeTextMutation(baseId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      idempotencyKey,
      text,
      title,
    }: {
      idempotencyKey: string;
      title: string;
      text: string;
    }) => importKnowledgeText(baseId, title, text, idempotencyKey),
    onSuccess: async () => {
      await invalidateKnowledgeCaches(queryClient, baseId);
    },
  });
}

export function useUploadKnowledgeDocumentMutation(baseId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      file,
      idempotencyKey,
      title,
    }: {
      file: File;
      idempotencyKey: string;
      title: string;
    }) => uploadKnowledgeDocument(baseId, title, file, idempotencyKey),
    onSuccess: async () => {
      await invalidateKnowledgeCaches(queryClient, baseId);
    },
  });
}

export function useReplaceKnowledgeTextMutation(
  baseId: string,
  documentId: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      idempotencyKey,
      text,
      title,
    }: {
      idempotencyKey: string;
      title: string;
      text: string;
    }) => replaceKnowledgeText(
      baseId,
      documentId,
      title,
      text,
      idempotencyKey,
    ),
    onSuccess: async () => {
      await invalidateDocumentCaches(
        queryClient,
        baseId,
        documentId,
      );
    },
  });
}

export function useReplaceKnowledgeFileMutation(
  baseId: string,
  documentId: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      file,
      idempotencyKey,
    }: {
      file: File;
      idempotencyKey: string;
    }) => replaceKnowledgeFile(baseId, documentId, file, idempotencyKey),
    onSuccess: async () => {
      await invalidateDocumentCaches(
        queryClient,
        baseId,
        documentId,
      );
    },
  });
}

export function useRenameKnowledgeDocumentMutation(baseId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      documentId,
      title,
    }: {
      documentId: string;
      title: string;
    }) => renameKnowledgeDocument(baseId, documentId, title),
    onSuccess: async () => {
      await invalidateKnowledgeCaches(queryClient, baseId);
    },
  });
}

export function useDeleteKnowledgeDocumentMutation(baseId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (documentId: string) =>
      deleteKnowledgeDocument(baseId, documentId),
    onSuccess: async (_result, documentId) => {
      queryClient.removeQueries({
        queryKey: knowledgeKeys.versions(baseId, documentId),
      });
      await invalidateKnowledgeCaches(queryClient, baseId);
    },
  });
}

export function useRetryKnowledgeVersionMutation(
  baseId: string,
  documentId: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (versionId: string) =>
      retryKnowledgeVersion(baseId, documentId, versionId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: knowledgeKeys.versions(baseId, documentId),
      });
      await invalidateKnowledgeCaches(queryClient, baseId);
    },
  });
}

export function useSearchKnowledgeMutation(baseId: string) {
  return useMutation({
    mutationFn: ({
      limit,
      query,
    }: {
      limit: number;
      query: string;
    }) => searchKnowledgeBase(baseId, query, limit),
  });
}

export function useProjectKnowledgeBases(projectId: string) {
  return useQuery({
    queryKey: knowledgeKeys.project(projectId),
    queryFn: () => listProjectKnowledgeBases(projectId),
  });
}

export function useBindProjectKnowledgeMutation(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (baseId: string) =>
      bindProjectKnowledgeBase(projectId, baseId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: knowledgeKeys.project(projectId),
      });
    },
    onError: async () => {
      // 上限或并发冲突后重新读取关联列表，避免前端继续展示过期数量。
      await queryClient.invalidateQueries({
        queryKey: knowledgeKeys.project(projectId),
      });
    },
  });
}

export function useUnbindProjectKnowledgeMutation(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (baseId: string) =>
      unbindProjectKnowledgeBase(projectId, baseId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: knowledgeKeys.project(projectId),
      });
    },
  });
}
