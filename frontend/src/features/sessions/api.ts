import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
} from "../../shared/api/client";
import {
  normalizeSessionSearchQuery,
  type Session,
  type SessionCreateInput,
  type SessionListQuery,
  type SessionListResponse,
  type SessionUpdateInput,
} from "./model";

export function listSessions(
  query: SessionListQuery = {},
): Promise<SessionListResponse> {
  // 保持与既有查询参数顺序一致，仅在有效传入时序列化，避免多余空查询项。
  const queryParams: {
    page?: number;
    page_size?: number;
    project_id?: string;
    unbound?: boolean;
    q?: string;
  } = {};
  if (query.page !== undefined) {
    queryParams.page = query.page;
  }
  if (query.pageSize !== undefined) {
    queryParams.page_size = query.pageSize;
  }
  if (query.projectId !== undefined) {
    queryParams.project_id = query.projectId;
  }
  if (query.unbound) {
    queryParams.unbound = true;
  }
  const normalizedQ = normalizeSessionSearchQuery(query.q);
  if (normalizedQ) {
    queryParams.q = normalizedQ;
  }
  return apiGet("/api/v1/sessions", {
    params: { query: queryParams },
  });
}

export function getSession(sessionId: string): Promise<Session> {
  return apiGet("/api/v1/sessions/{session_id}", {
    params: { path: { session_id: sessionId } },
  });
}

export function createSession(input: SessionCreateInput): Promise<Session> {
  return apiPost("/api/v1/sessions", {
    body: input,
  });
}

export function updateSession(
  sessionId: string,
  input: SessionUpdateInput,
): Promise<Session> {
  return apiPatch("/api/v1/sessions/{session_id}", {
    body: input,
    params: { path: { session_id: sessionId } },
  });
}

export function deleteSession(sessionId: string): Promise<void> {
  return apiDelete("/api/v1/sessions/{session_id}", {
    params: { path: { session_id: sessionId } },
  });
}
