import { apiRequest } from "../../shared/api/client";
import type {
  Session,
  SessionCreateInput,
  SessionListQuery,
  SessionListResponse,
  SessionUpdateInput,
} from "./model";

function toSessionListQuery(query: SessionListQuery): string {
  const params = new URLSearchParams();
  if (query.page !== undefined) {
    params.set("page", String(query.page));
  }
  if (query.pageSize !== undefined) {
    params.set("page_size", String(query.pageSize));
  }
  if (query.projectId !== undefined) {
    params.set("project_id", query.projectId);
  }
  if (query.unbound) {
    params.set("unbound", "true");
  }
  const serialized = params.toString();
  return serialized ? `?${serialized}` : "";
}

export function listSessions(
  query: SessionListQuery = {},
): Promise<SessionListResponse> {
  return apiRequest<SessionListResponse>(
    `/sessions${toSessionListQuery(query)}`,
    { method: "GET" },
  );
}

export function getSession(sessionId: string): Promise<Session> {
  return apiRequest<Session>(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: "GET",
  });
}

export function createSession(input: SessionCreateInput): Promise<Session> {
  return apiRequest<Session>("/sessions", {
    method: "POST",
    body: input,
  });
}

export function updateSession(
  sessionId: string,
  input: SessionUpdateInput,
): Promise<Session> {
  return apiRequest<Session>(
    `/sessions/${encodeURIComponent(sessionId)}`,
    {
      method: "PATCH",
      body: input,
    },
  );
}

export function deleteSession(sessionId: string): Promise<void> {
  return apiRequest<void>(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
}

