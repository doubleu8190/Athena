import { countCodePoints } from "../../shared/lib/text";
import { isDeptAdmin, isSuperAdmin, type User } from "../auth/model";
import type { VisibilityScope } from "../../shared/ui/VisibilityBadge";

export type VisibilityGrantTargetType = "department" | "role";

export interface VisibilityGrantInput {
  include_subdepartments: boolean;
  target_id: string;
  target_type: VisibilityGrantTargetType;
}

export interface VisibilityGrant extends VisibilityGrantInput {
  created_at: string;
  id: string;
}

export const KNOWLEDGE_BASE_NAME_MAX_LENGTH = 120;
export const KNOWLEDGE_DESCRIPTION_MAX_LENGTH = 500;
export const DOCUMENT_TITLE_MAX_LENGTH = 255;
export const KNOWLEDGE_TEXT_MAX_LENGTH = 1_000_000;
export const KNOWLEDGE_FILE_MAX_BYTES = 20 * 1024 * 1024;
export const PROJECT_KNOWLEDGE_LIMIT = 20;
export const KNOWLEDGE_SEARCH_DEFAULT_LIMIT = 8;

export const KNOWLEDGE_FILE_ACCEPT = [
  ".txt",
  ".md",
  ".pdf",
  ".docx",
  ".xlsx",
  ".pptx",
  ".png",
  ".jpg",
  ".jpeg",
].join(",");

const SUPPORTED_EXTENSIONS = new Set(
  KNOWLEDGE_FILE_ACCEPT.split(","),
);
const LEGACY_OFFICE_EXTENSIONS = new Set([".doc", ".xls", ".ppt"]);
const CONTROL_CHARACTER_PATTERN = /\p{Cc}/u;

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string | null;
  visibility_scope: VisibilityScope;
  owner_department_id: string | null;
  visibility_grants: VisibilityGrant[];
  created_at: string;
  updated_at: string;
}

/**
 * 判断当前登录用户是否拥有目标知识库的管理写权限（编辑、删除、资料上传与版本变更）：
 * - personal：仅本人可见且可管理；
 * - company：仅 super_admin 可管理；
 * - department：super_admin 或管辖该 owner_department_id 的 dept_admin 可管理。
 */
export function canManageKnowledgeBase(
  user: Pick<User, "role" | "managed_department_ids"> | null | undefined,
  base: Pick<KnowledgeBase, "visibility_scope" | "owner_department_id">,
): boolean {
  // 未登录或异常空用户态一律 fail-closed 拒绝写权限。
  if (!user) {
    return false;
  }
  const scope = base.visibility_scope;
  if (scope === "personal") {
    return true;
  }
  if (isSuperAdmin(user)) {
    return true;
  }
  if (scope === "department") {
    return (
      isDeptAdmin(user) &&
      Boolean(
        base.owner_department_id &&
          user.managed_department_ids.includes(base.owner_department_id),
      )
    );
  }
  return false;
}

/**
 * 统计部门共享知识库除归属部门默认授权以外的额外授权数量（按 target_type:target_id 去重）：
 * - 非 department 范围或缺少归属部门时返回 0；
 * - 跳过 target_type === "department" 且 target_id === owner_department_id 的归属默认项；
 * - 供目录卡片与详情页克制展示「额外共享 N 项」徽章。
 */
export function countExtraVisibilityGrants(
  base: Pick<
    KnowledgeBase,
    "visibility_scope" | "owner_department_id" | "visibility_grants"
  >,
): number {
  if (base.visibility_scope !== "department") {
    return 0;
  }
  const ownerDeptId = base.owner_department_id?.trim() ?? "";
  if (!ownerDeptId || base.visibility_grants.length === 0) {
    return 0;
  }
  const seen = new Set<string>([`department:${ownerDeptId}`]);
  let count = 0;
  for (const grant of base.visibility_grants) {
    const targetId = grant.target_id.trim();
    if (!targetId) {
      continue;
    }
    const key = `${grant.target_type}:${targetId}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    count += 1;
  }
  return count;
}

export interface KnowledgeBaseListResponse {
  items: KnowledgeBase[];
  page: number;
  page_size: number;
}

export interface KnowledgeListQuery {
  ownerDepartmentId?: string;
  page?: number;
  pageSize?: number;
  visibilityScope?: VisibilityScope;
}

export interface KnowledgeBaseInput {
  description?: string;
  name: string;
  owner_department_id?: string | null;
  visibility_grants?: VisibilityGrantInput[];
  visibility_scope?: VisibilityScope;
}

export interface KnowledgeBasePatch {
  description?: string;
  name?: string;
  owner_department_id?: string | null;
  visibility_grants?: VisibilityGrantInput[];
  visibility_scope?: VisibilityScope;
}

export type KnowledgeDocumentState =
  | "queued"
  | "processing"
  | "ready"
  | "failed";

export interface KnowledgeDocument {
  id: string;
  knowledge_base_id: string;
  title: string;
  active_version_id: string | null;
  pending_version_id: string | null;
  /** 文档列表与详情接口始终附带的最新版本摘要；尚无版本记录时为 null。 */
  latest_version: KnowledgeVersion | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeDocumentListResponse {
  items: KnowledgeDocument[];
  page: number;
  page_size: number;
}

export interface KnowledgeDocumentListQuery {
  page?: number;
  pageSize?: number;
}

export interface KnowledgeVersion {
  id: string;
  document_id: string;
  state: KnowledgeDocumentState;
  error_code: string | null;
  warnings: string[];
  chunk_count: number;
  created_at: string;
  completed_at: string | null;
}

export interface KnowledgeImportResponse {
  document_id: string;
  version_id: string;
  state: string;
  status_url: string;
}

export interface KnowledgeLocator {
  [key: string]: unknown;
}

export interface KnowledgeSearchHit {
  chunk_id: string;
  chunk_ids: string[];
  document_id: string;
  version_id: string;
  title: string;
  text: string;
  locators: KnowledgeLocator[];
  channels: string[];
}

export interface KnowledgeSearchResponse {
  hits: KnowledgeSearchHit[];
  channels: string[];
  degraded_reasons: string[];
}

export interface KnowledgeTextInput {
  title: string;
  text: string;
}

export function countUnicodeCodePoints(value: string): number {
  return countCodePoints(value);
}

export function normalizeKnowledgeName(value: string): string {
  return value.trim();
}

export function validateKnowledgeName(value: string): string | null {
  const normalized = normalizeKnowledgeName(value);
  if (!normalized) {
    return "请输入知识库名称。";
  }
  if (
    countUnicodeCodePoints(normalized) > KNOWLEDGE_BASE_NAME_MAX_LENGTH ||
    CONTROL_CHARACTER_PATTERN.test(normalized)
  ) {
    return "知识库名称需为 1–120 个字符，且不能包含控制字符。";
  }
  return null;
}

export function normalizeKnowledgeDescription(value: string): string {
  return value.trim();
}

export function validateKnowledgeDescription(
  value: string,
  existingDescription: string | null = null,
): string | null {
  const normalized = normalizeKnowledgeDescription(value);
  if (!normalized) {
    if (existingDescription) {
      return "当前接口暂不支持清空知识库说明，请保留原说明或填写新说明。";
    }
    return null;
  }
  if (
    countUnicodeCodePoints(normalized) >
      KNOWLEDGE_DESCRIPTION_MAX_LENGTH ||
    CONTROL_CHARACTER_PATTERN.test(normalized)
  ) {
    return "知识库说明最多 500 个字符，且不能包含换行或控制字符。";
  }
  return null;
}

export function normalizeDocumentTitle(value: string): string {
  return value.trim();
}

export function validateDocumentTitle(value: string): string | null {
  const normalized = normalizeDocumentTitle(value);
  if (!normalized) {
    return "请输入文档标题。";
  }
  if (
    countUnicodeCodePoints(normalized) > DOCUMENT_TITLE_MAX_LENGTH ||
    CONTROL_CHARACTER_PATTERN.test(normalized)
  ) {
    return "文档标题需为 1–255 个字符，且不能包含控制字符。";
  }
  return null;
}

export function validateKnowledgeText(value: string): string | null {
  const normalized = value.trim();
  if (!normalized) {
    return "请输入要导入的文本。";
  }
  if (countUnicodeCodePoints(normalized) > KNOWLEDGE_TEXT_MAX_LENGTH) {
    return "文本内容不能超过 1,000,000 个字符。";
  }
  return null;
}

export function validateKnowledgeSearchQuery(value: string): string | null {
  const normalized = value.trim();
  if (!normalized) {
    return "请输入检索内容。";
  }
  if (countUnicodeCodePoints(normalized) > 4_000) {
    return "检索内容不能超过 4,000 个字符。";
  }
  return null;
}

function fileExtension(filename: string): string {
  const normalized = filename.trim().toLowerCase();
  const dot = normalized.lastIndexOf(".");
  return dot >= 0 ? normalized.slice(dot) : "";
}

export function validateKnowledgeFile(file: File): string | null {
  const extension = fileExtension(file.name);
  if (LEGACY_OFFICE_EXTENSIONS.has(extension)) {
    return "不支持旧版 Office 文件，请转换为 DOCX、XLSX 或 PPTX 后上传。";
  }
  if (!SUPPORTED_EXTENSIONS.has(extension)) {
    return "支持 TXT、MD、PDF、DOCX、XLSX、PPTX、PNG、JPG、JPEG 文件。";
  }
  if (file.size <= 0) {
    return "请选择非空文件。";
  }
  if (file.size > KNOWLEDGE_FILE_MAX_BYTES) {
    return "文件不能超过 20 MiB。";
  }
  return null;
}

export function defaultDocumentTitle(filename: string): string {
  const name = filename.split(/[\\/]/).pop()?.trim() ?? "";
  const dot = name.lastIndexOf(".");
  const withoutExtension = dot > 0 ? name.slice(0, dot) : name;
  return Array.from(withoutExtension)
    .slice(0, DOCUMENT_TITLE_MAX_LENGTH)
    .join("");
}

export function documentStateLabel(
  state: KnowledgeDocumentState | null,
): string {
  switch (state) {
    case "queued":
      return "排队中";
    case "processing":
      return "处理中";
    case "ready":
      return "已就绪";
    case "failed":
      return "处理失败";
    default:
      return "状态未知";
  }
}
