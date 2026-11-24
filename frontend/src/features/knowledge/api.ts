import {
  apiDownload,
  apiMultipart,
  apiRequest,
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

function toListQuery(query: KnowledgeListQuery): string {
  const params = new URLSearchParams();
  if (query.page !== undefined) {
    params.set("page", String(query.page));
  }
  if (query.pageSize !== undefined) {
    params.set("page_size", String(query.pageSize));
  }
  if (query.visibilityScope !== undefined) {
    params.set("visibility_scope", query.visibilityScope);
  }
  if (query.ownerDepartmentId !== undefined && query.ownerDepartmentId.trim()) {
    params.set("owner_department_id", query.ownerDepartmentId.trim());
  }
  const serialized = params.toString();
  return serialized ? `?${serialized}` : "";
}

function toDocumentListQuery(
  query: KnowledgeDocumentListQuery,
): string {
  return toListQuery(query);
}

function idempotencyHeaders(key: string): HeadersInit {
  return { "Idempotency-Key": key };
}

export function listKnowledgeBases(
  query: KnowledgeListQuery = {},
): Promise<KnowledgeBaseListResponse> {
  return apiRequest<KnowledgeBaseListResponse>(
    `/knowledge-bases${toListQuery(query)}`,
    { method: "GET" },
  );
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
  return apiRequest<KnowledgeBase>(basePath(baseId), { method: "GET" });
}

export function createKnowledgeBase(
  input: KnowledgeBaseInput,
): Promise<KnowledgeBase> {
  return apiRequest<KnowledgeBase>("/knowledge-bases", {
    method: "POST",
    body: input,
  });
}

export function updateKnowledgeBase(
  baseId: string,
  input: KnowledgeBasePatch,
): Promise<KnowledgeBase> {
  return apiRequest<KnowledgeBase>(basePath(baseId), {
    method: "PATCH",
    body: input,
  });
}

export function deleteKnowledgeBase(baseId: string): Promise<void> {
  return apiRequest<void>(basePath(baseId), { method: "DELETE" });
}

export function listKnowledgeDocuments(
  baseId: string,
  query: KnowledgeDocumentListQuery = {},
): Promise<KnowledgeDocumentListResponse> {
  return apiRequest<KnowledgeDocumentListResponse>(
    `${basePath(baseId)}/documents${toDocumentListQuery(query)}`,
    { method: "GET" },
  );
}

export function getKnowledgeDocument(
  baseId: string,
  documentId: string,
): Promise<KnowledgeDocument> {
  return apiRequest<KnowledgeDocument>(documentPath(baseId, documentId), {
    method: "GET",
  });
}

export function listKnowledgeVersions(
  baseId: string,
  documentId: string,
): Promise<KnowledgeVersion[]> {
  return apiRequest<KnowledgeVersion[]>(
    `${documentPath(baseId, documentId)}/versions`,
    { method: "GET" },
  );
}

export function importKnowledgeText(
  baseId: string,
  title: string,
  text: string,
  idempotencyKey: string,
): Promise<KnowledgeImportResponse> {
  return apiRequest<KnowledgeImportResponse>(
    `${basePath(baseId)}/documents/text`,
    {
      method: "POST",
      body: { title, text },
      headers: idempotencyHeaders(idempotencyKey),
    },
  );
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
  return apiMultipart<KnowledgeImportResponse>(
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
  return apiRequest<KnowledgeImportResponse>(
    `${documentPath(baseId, documentId)}/versions/text`,
    {
      method: "POST",
      body: { title, text },
      headers: idempotencyHeaders(idempotencyKey),
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
  return apiMultipart<KnowledgeImportResponse>(
    `${documentPath(baseId, documentId)}/versions/upload`,
    form,
    { method: "POST", headers: idempotencyHeaders(idempotencyKey) },
  );
}

export function renameKnowledgeDocument(
  baseId: string,
  documentId: string,
  title: string,
): Promise<KnowledgeDocument> {
  return apiRequest<KnowledgeDocument>(documentPath(baseId, documentId), {
    method: "PATCH",
    body: { title },
  });
}

export function deleteKnowledgeDocument(
  baseId: string,
  documentId: string,
): Promise<void> {
  return apiRequest<void>(documentPath(baseId, documentId), {
    method: "DELETE",
  });
}

export function retryKnowledgeVersion(
  baseId: string,
  documentId: string,
  versionId: string,
): Promise<KnowledgeVersion> {
  return apiRequest<KnowledgeVersion>(
    `${documentPath(baseId, documentId)}/versions/${encodeURIComponent(
      versionId,
    )}/retry`,
    { method: "POST" },
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
  return apiRequest<KnowledgeSearchResponse>(`${basePath(baseId)}/search`, {
    method: "POST",
    body: { query, limit },
  });
}

export function getKnowledgeChunk(
  baseId: string,
  chunkId: string,
): Promise<KnowledgeSearchHit> {
  return apiRequest<KnowledgeSearchHit>(
    `${basePath(baseId)}/chunks/${encodeURIComponent(chunkId)}`,
    { method: "GET" },
  );
}

export function listProjectKnowledgeBases(
  projectId: string,
): Promise<KnowledgeBase[]> {
  return apiRequest<KnowledgeBase[]>(
    `/projects/${encodeURIComponent(projectId)}/knowledge-bases`,
    { method: "GET" },
  );
}

export function bindProjectKnowledgeBase(
  projectId: string,
  baseId: string,
): Promise<void> {
  return apiRequest<void>(
    `/projects/${encodeURIComponent(projectId)}/knowledge-bases/${encodeURIComponent(
      baseId,
    )}`,
    { method: "PUT" },
  );
}

export function unbindProjectKnowledgeBase(
  projectId: string,
  baseId: string,
): Promise<void> {
  return apiRequest<void>(
    `/projects/${encodeURIComponent(projectId)}/knowledge-bases/${encodeURIComponent(
      baseId,
    )}`,
    { method: "DELETE" },
  );
}
