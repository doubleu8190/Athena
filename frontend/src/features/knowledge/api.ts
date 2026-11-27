import {
  apiDelete,
  apiDownload,
  apiGet,
  apiMultipart,
  apiPatch,
  apiPost,
  apiPut,
  type ApiSchemas,
  type DownloadedFile,
} from "../../shared/api/client";
import type {
  KnowledgeBase,
  KnowledgeBaseInput,
  KnowledgeBaseListResponse,
  KnowledgeBasePatch,
  KnowledgeDocument,
  KnowledgeDocumentListQuery,
  KnowledgeDocumentListResponse,
  KnowledgeDocumentState,
  KnowledgeImportResponse,
  KnowledgeListQuery,
  KnowledgeSearchHit,
  KnowledgeSearchResponse,
  KnowledgeVersion,
} from "./model";

function basePath(baseId: string): string {
  return `/knowledge-bases/${encodeURIComponent(baseId)}`;
}

function documentPath(baseId: string, documentId: string): string {
  return `${basePath(baseId)}/documents/${encodeURIComponent(documentId)}`;
}

function idempotencyHeaders(key: string): HeadersInit {
  return { "Idempotency-Key": key };
}

function toKnowledgeDocumentState(value: string): KnowledgeDocumentState {
  if (
    value === "queued" ||
    value === "processing" ||
    value === "ready" ||
    value === "failed"
  ) {
    return value;
  }
  return "failed";
}

function toKnowledgeVersion(
  raw: ApiSchemas["KnowledgeVersionResponse"],
): KnowledgeVersion {
  return {
    ...raw,
    state: toKnowledgeDocumentState(raw.state),
  };
}

function toKnowledgeDocument(
  raw: ApiSchemas["KnowledgeDocumentResponse"],
): KnowledgeDocument {
  return {
    ...raw,
    latest_version:
      raw.latest_version === null
        ? null
        : toKnowledgeVersion(raw.latest_version),
  };
}

export function listKnowledgeBases(
  query: KnowledgeListQuery = {},
): Promise<KnowledgeBaseListResponse> {
  const queryParams: {
    page?: number;
    page_size?: number;
    visibility_scope?: "company" | "department" | "personal";
    owner_department_id?: string;
  } = {};
  if (query.page !== undefined) {
    queryParams.page = query.page;
  }
  if (query.pageSize !== undefined) {
    queryParams.page_size = query.pageSize;
  }
  if (query.visibilityScope !== undefined) {
    queryParams.visibility_scope = query.visibilityScope;
  }
  if (query.ownerDepartmentId !== undefined && query.ownerDepartmentId.trim()) {
    queryParams.owner_department_id = query.ownerDepartmentId.trim();
  }
  return apiGet("/api/v1/knowledge-bases", {
    params: { query: queryParams },
  });
}

export async function listAllKnowledgeBases(): Promise<KnowledgeBase[]> {
  const pageSize = 100;
  const bases: KnowledgeBase[] = [];
  let page = 1;
  while (true) {
    const result = await listKnowledgeBases({ page, pageSize });
    bases.push(...result.items);
    if (result.items.length < pageSize) {
      break;
    }
    page += 1;
  }
  return bases;
}

export function getKnowledgeBase(baseId: string): Promise<KnowledgeBase> {
  return apiGet("/api/v1/knowledge-bases/{base_id}", {
    params: { path: { base_id: baseId } },
  });
}

export function createKnowledgeBase(
  input: KnowledgeBaseInput,
): Promise<KnowledgeBase> {
  return apiPost("/api/v1/knowledge-bases", {
    body: input,
  });
}

export function updateKnowledgeBase(
  baseId: string,
  input: KnowledgeBasePatch,
): Promise<KnowledgeBase> {
  return apiPatch("/api/v1/knowledge-bases/{base_id}", {
    body: input,
    params: { path: { base_id: baseId } },
  });
}

export function deleteKnowledgeBase(baseId: string): Promise<void> {
  return apiDelete("/api/v1/knowledge-bases/{base_id}", {
    params: { path: { base_id: baseId } },
  });
}

export async function listKnowledgeDocuments(
  baseId: string,
  query: KnowledgeDocumentListQuery = {},
): Promise<KnowledgeDocumentListResponse> {
  const queryParams: { page?: number; page_size?: number } = {};
  if (query.page !== undefined) {
    queryParams.page = query.page;
  }
  if (query.pageSize !== undefined) {
    queryParams.page_size = query.pageSize;
  }
  const response = await apiGet("/api/v1/knowledge-bases/{base_id}/documents", {
    params: {
      path: { base_id: baseId },
      query: queryParams,
    },
  });
  return {
    ...response,
    items: response.items.map(toKnowledgeDocument),
  };
}

export async function listKnowledgeVersions(
  baseId: string,
  documentId: string,
): Promise<KnowledgeVersion[]> {
  const response = await apiGet(
    "/api/v1/knowledge-bases/{base_id}/documents/{document_id}/versions",
    {
      params: { path: { base_id: baseId, document_id: documentId } },
    },
  );
  return response.map(toKnowledgeVersion);
}

export function importKnowledgeText(
  baseId: string,
  title: string,
  text: string,
  idempotencyKey: string,
): Promise<KnowledgeImportResponse> {
  return apiPost("/api/v1/knowledge-bases/{base_id}/documents/text", {
    body: { title, text },
    params: {
      header: { "Idempotency-Key": idempotencyKey },
      path: { base_id: baseId },
    },
  });
}

export function uploadKnowledgeDocument(
  baseId: string,
  title: string,
  file: File,
  idempotencyKey: string,
): Promise<KnowledgeImportResponse> {
  const form = new FormData();
  form.set("title", title);
  form.set("file", file);
  return apiMultipart<ApiSchemas["KnowledgeImportResponse"]>(
    `${basePath(baseId)}/documents/upload`,
    form,
    { method: "POST", headers: idempotencyHeaders(idempotencyKey) },
  );
}

export function replaceKnowledgeText(
  baseId: string,
  documentId: string,
  title: string,
  text: string,
  idempotencyKey: string,
): Promise<KnowledgeImportResponse> {
  return apiPost(
    "/api/v1/knowledge-bases/{base_id}/documents/{document_id}/versions/text",
    {
      body: { title, text },
      params: {
        header: { "Idempotency-Key": idempotencyKey },
        path: { base_id: baseId, document_id: documentId },
      },
    },
  );
}

export function replaceKnowledgeFile(
  baseId: string,
  documentId: string,
  file: File,
  idempotencyKey: string,
): Promise<KnowledgeImportResponse> {
  const form = new FormData();
  form.set("file", file);
  return apiMultipart<ApiSchemas["KnowledgeImportResponse"]>(
    `${documentPath(baseId, documentId)}/versions/upload`,
    form,
    { method: "POST", headers: idempotencyHeaders(idempotencyKey) },
  );
}

export async function renameKnowledgeDocument(
  baseId: string,
  documentId: string,
  title: string,
): Promise<KnowledgeDocument> {
  return toKnowledgeDocument(
    await apiPatch(
      "/api/v1/knowledge-bases/{base_id}/documents/{document_id}",
      {
        body: { title },
        params: { path: { base_id: baseId, document_id: documentId } },
      },
    ),
  );
}

export function deleteKnowledgeDocument(
  baseId: string,
  documentId: string,
): Promise<void> {
  return apiDelete(
    "/api/v1/knowledge-bases/{base_id}/documents/{document_id}",
    {
      params: { path: { base_id: baseId, document_id: documentId } },
    },
  );
}

export async function retryKnowledgeVersion(
  baseId: string,
  documentId: string,
  versionId: string,
): Promise<KnowledgeVersion> {
  return toKnowledgeVersion(
    await apiPost(
      "/api/v1/knowledge-bases/{base_id}/documents/{document_id}/versions/{version_id}/retry",
      {
        params: {
          path: {
            base_id: baseId,
            document_id: documentId,
            version_id: versionId,
          },
        },
      },
    ),
  );
}

export function downloadKnowledgeSource(
  baseId: string,
  documentId: string,
  versionId: string,
): Promise<DownloadedFile> {
  return apiDownload(
    `${documentPath(baseId, documentId)}/versions/${encodeURIComponent(
      versionId,
    )}/source`,
    { method: "GET" },
  );
}

export function searchKnowledgeBase(
  baseId: string,
  query: string,
  limit: number,
): Promise<KnowledgeSearchResponse> {
  return apiPost("/api/v1/knowledge-bases/{base_id}/search", {
    body: { query, limit },
    params: { path: { base_id: baseId } },
  });
}

export function getKnowledgeChunk(
  baseId: string,
  chunkId: string,
): Promise<KnowledgeSearchHit> {
  return apiGet("/api/v1/knowledge-bases/{base_id}/chunks/{chunk_id}", {
    params: { path: { base_id: baseId, chunk_id: chunkId } },
  });
}

export function listProjectKnowledgeBases(
  projectId: string,
): Promise<KnowledgeBase[]> {
  return apiGet("/api/v1/projects/{project_id}/knowledge-bases", {
    params: { path: { project_id: projectId } },
  });
}

export function bindProjectKnowledgeBase(
  projectId: string,
  baseId: string,
): Promise<void> {
  return apiPut("/api/v1/projects/{project_id}/knowledge-bases/{base_id}", {
    params: { path: { base_id: baseId, project_id: projectId } },
  });
}

export function unbindProjectKnowledgeBase(
  projectId: string,
  baseId: string,
): Promise<void> {
  return apiDelete("/api/v1/projects/{project_id}/knowledge-bases/{base_id}", {
    params: { path: { base_id: baseId, project_id: projectId } },
  });
}
