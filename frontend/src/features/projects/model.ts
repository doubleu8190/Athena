export const PROJECT_NAME_MAX_LENGTH = 120;

export interface Project {
  created_at: string;
  id: string;
  name: string;
  updated_at: string;
}

export interface ProjectListResponse {
  items: Project[];
  page: number;
  page_size: number;
  total: number;
}

export interface ProjectListQuery {
  page?: number;
  pageSize?: number;
}

export interface ProjectNameInput {
  name: string;
}

const CONTROL_CHARACTER_PATTERN = /\p{Cc}/u;

export function normalizeProjectName(value: string): string {
  return value.trim();
}

export function validateProjectName(value: string): string | null {
  const normalized = normalizeProjectName(value);
  if (!normalized) {
    return "请输入项目名称。";
  }
  if (
    Array.from(normalized).length > PROJECT_NAME_MAX_LENGTH ||
    CONTROL_CHARACTER_PATTERN.test(normalized)
  ) {
    return "项目名称需为 1–120 个字符，且不能包含控制字符。";
  }
  return null;
}

