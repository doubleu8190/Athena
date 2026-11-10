export const SESSION_TITLE_MAX_LENGTH = 120;

export interface Session {
  created_at: string;
  id: string;
  project_id: string | null;
  title: string | null;
  updated_at: string;
}

export interface SessionListResponse {
  items: Session[];
  page: number;
  page_size: number;
  total: number;
}

export interface SessionListQuery {
  page?: number;
  pageSize?: number;
  projectId?: string;
  unbound?: boolean;
}

export interface SessionCreateInput {
  project_id?: string | null;
  title?: string | null;
}

export interface SessionUpdateInput {
  project_id?: string | null;
  title?: string | null;
}

const CONTROL_CHARACTER_PATTERN = /\p{Cc}/u;

export function normalizeSessionTitle(value: string): string {
  return value.trim();
}

export function validateSessionTitle(value: string): string | null {
  const normalized = normalizeSessionTitle(value);
  if (!normalized) {
    return "请输入会话标题。";
  }
  if (
    Array.from(normalized).length > SESSION_TITLE_MAX_LENGTH ||
    CONTROL_CHARACTER_PATTERN.test(normalized)
  ) {
    return "会话标题需为 1–120 个字符，且不能包含控制字符。";
  }
  return null;
}

export function sessionDisplayTitle(title: string | null): string {
  return title?.trim() || "未命名对话";
}

