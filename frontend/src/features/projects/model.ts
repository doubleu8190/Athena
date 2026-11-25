import { countCodePoints } from "../../shared/lib/text";

export const PROJECT_NAME_MAX_LENGTH = 120;
export const PROJECT_DESCRIPTION_MAX_LENGTH = 500;
export const PROJECT_INSTRUCTIONS_MAX_LENGTH = 8000;
export const PROJECT_DEFAULT_SKILLS_MAX = 20;

export interface Project {
  created_at: string;
  /** 项目默认挂载技能 ID 列表（可选兼容旧夹具，消费时统一 ?? []）。 */
  default_skill_ids?: string[];
  /** 可选项目简介（最大 500 字，消费时统一 ?? null）。 */
  description?: string | null;
  id: string;
  /** 可选项目专属背景指令（最大 8,000 字，仅注入主对话，消费时统一 ?? null）。 */
  instructions?: string | null;
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

export interface ProjectCreateInput {
  default_skill_ids?: string[];
  description?: string | null;
  instructions?: string | null;
  name: string;
}

export interface ProjectUpdateInput {
  default_skill_ids?: string[];
  description?: string | null;
  instructions?: string | null;
  name?: string;
}

const CONTROL_CHARACTER_PATTERN = /\p{Cc}/u;

export function countProjectCodePoints(value: string): number {
  return countCodePoints(value);
}

function hasDisallowedMultilineControlChar(value: string): boolean {
  for (const character of value) {
    if (
      CONTROL_CHARACTER_PATTERN.test(character) &&
      character !== "\n" &&
      character !== "\r" &&
      character !== "\t"
    ) {
      return true;
    }
  }
  return false;
}

export function normalizeProjectName(value: string): string {
  return value.trim();
}

export function validateProjectName(value: string): string | null {
  if (CONTROL_CHARACTER_PATTERN.test(value)) {
    return "项目名称需为 1–120 个字符，且不能包含控制字符。";
  }
  const normalized = normalizeProjectName(value);
  if (!normalized) {
    return "请输入项目名称。";
  }
  if (countProjectCodePoints(normalized) > PROJECT_NAME_MAX_LENGTH) {
    return "项目名称需为 1–120 个字符，且不能包含控制字符。";
  }
  return null;
}

export function normalizeProjectDescription(value: string): string | null {
  const normalized = value.trim();
  return normalized ? normalized : null;
}

/**
 * 校验项目简介：与后端 _normalize_description 对齐，拒绝所有 Cc 控制字符（含换行），
 * 最大 500 个 Unicode 码点，允许纯空白清空为 null。
 */
export function validateProjectDescription(value: string): string | null {
  if (CONTROL_CHARACTER_PATTERN.test(value)) {
    return "项目简介最多 500 个字符，且不能包含换行或控制字符。";
  }
  const normalized = value.trim();
  if (!normalized) {
    return null;
  }
  if (countProjectCodePoints(normalized) > PROJECT_DESCRIPTION_MAX_LENGTH) {
    return "项目简介最多 500 个字符，且不能包含换行或控制字符。";
  }
  return null;
}

export function normalizeProjectInstructions(value: string): string | null {
  const normalized = value.trim();
  return normalized ? normalized : null;
}

/**
 * 校验项目专属背景指令：与后端 _normalize_instructions 对齐，允许换行/回车/制表符，
 * 拒绝其他 Cc 控制字符，最大 8,000 个 Unicode 码点，允许空字符串清空。
 */
export function validateProjectInstructions(value: string): string | null {
  if (hasDisallowedMultilineControlChar(value)) {
    return "专属背景指令不能包含除换行与制表符以外的控制字符。";
  }
  const normalized = value.trim();
  if (!normalized) {
    return null;
  }
  if (countProjectCodePoints(normalized) > PROJECT_INSTRUCTIONS_MAX_LENGTH) {
    return "专属背景指令最多 8,000 个字符。";
  }
  return null;
}
