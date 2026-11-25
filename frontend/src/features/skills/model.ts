import { countCodePoints } from "../../shared/lib/text";
import type {
  VisibilityGrant,
  VisibilityGrantInput,
} from "../knowledge/model";

export const SKILL_ZIP_MAX_BYTES = 25 * 1024 * 1024;
export const PERSONAL_SKILL_FILE_MAX_BYTES = 1024 * 1024;
export const PERSONAL_SKILL_NAME_MAX_LENGTH = 120;
export const PERSONAL_SKILL_DESCRIPTION_MAX_LENGTH = 1000;
export const PERSONAL_SKILL_GUIDE_MAX_LENGTH = 100_000;
export const SKILL_STARTER_PROMPT_MAX_LENGTH = 500;
export const SKILL_SLUG_MAX_LENGTH = 64;
export const SKILL_VERSION_LABEL_MAX_LENGTH = 64;
export const SKILL_SLUG_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;

export type SkillFormat = "markdown" | "package";
export type SkillScope = "company" | "department" | "personal";
/** 兼容后端历史与旧测试夹具可能出现的 "public" 枚举，运行时通过 skillScope() 统一归一为 "company"。 */
export type LegacyCompatibleSkillScope = SkillScope | "public";

export type DependencyCheckStatus =
  | "satisfied"
  | "dependency_unsatisfied"
  | "dependency_invalid";

export interface DependencyCheckItem {
  requirement: string;
  package: string;
  satisfied: boolean;
  installed_version: string | null;
  detail_code: string | null;
  marker_applies: boolean;
}

export interface DependencyCheck {
  status: DependencyCheckStatus;
  python_version: string;
  python_implementation: string;
  items: DependencyCheckItem[];
  checked_at: string;
}

export interface SkillScriptDefinition {
  path: string;
  label: string;
  purpose: string;
  parameters: Record<string, unknown>;
}

export interface SkillPackageResource {
  path: string;
  size_bytes: number;
  sha256: string;
  text: boolean;
}

export interface SkillPackageManifest {
  version: number;
  slug: string;
  name: string;
  description: string;
  dependencies: string[];
  guide: string;
  scripts: SkillScriptDefinition[];
  files: SkillPackageResource[];
}

export interface Skill {
  id: string;
  slug: string;
  name: string;
  description: string;
  current_version_id: string | null;
  current_version_label: string | null;
  enabled: boolean;
  script_requires_approval: boolean;
  revision: number;
  scope?: LegacyCompatibleSkillScope;
  owner_department_id?: string | null;
  visibility_grants?: VisibilityGrant[];
  /** M3.3 双形态字段（可选以兼容历史夹具缺省值）。 */
  format?: SkillFormat;
  has_scripts?: boolean;
  featured?: boolean;
  starter_prompt?: string | null;
  updated_at: string;
  dependency_check: DependencyCheck | null;
}

export interface SkillVersion {
  id: string;
  skill_id: string;
  ordinal: number;
  label: string;
  slug: string;
  name: string;
  description: string;
  guide: string;
  dependencies: string[];
  dependency_check: DependencyCheck;
  scripts: SkillScriptDefinition[];
  files: SkillPackageResource[];
  package_sha256: string;
  created_at: string;
}

export interface SkillVersionPage {
  items: SkillVersion[];
  total: number;
  page: number;
  page_size: number;
}

export interface SkillListResponse {
  items: Skill[];
}

export interface SkillAdminDetails extends Skill {
  versions: SkillVersion[];
}

export interface SkillConfigurationInput {
  current_version_id: string;
  enabled: boolean;
  expected_revision: number;
  featured?: boolean;
  script_requires_approval: boolean;
  starter_prompt?: string | null;
  visibility_grants?: VisibilityGrantInput[];
}

/** 契约 3.5：POST /api/v1/admin/skills/markdown（严格 extra="forbid"，不传多余字段）。 */
export interface AdminSkillMarkdownCreateInput {
  description: string;
  enabled: boolean;
  featured: boolean;
  guide: string;
  name: string;
  owner_department_id?: string | null;
  scope: "company" | "department";
  slug?: string | null;
  starter_prompt?: string | null;
  visibility_grants?: VisibilityGrantInput[] | null;
}

/** 契约 3.6：POST /api/v1/admin/skills/{skill_id}/versions/markdown（严格 extra="forbid"）。 */
export interface AdminSkillMarkdownVersionInput {
  activate: boolean;
  description: string;
  expected_revision: number;
  guide: string;
  label?: string | null;
  name: string;
}

export interface ImportSkillPackageOptions {
  ownerDepartmentId?: string;
  scope?: "company" | "department";
  visibilityGrants?: VisibilityGrantInput[];
}

export interface SkillFormState {
  currentVersionId: string;
  enabled: boolean;
  featured: boolean;
  scriptRequiresApproval: boolean;
  starterPrompt: string;
  visibilityGrants: VisibilityGrantInput[];
}

export interface PersonalSkillInput {
  name: string;
  description: string;
  guide: string;
}

export type PersonalSkillDraft = PersonalSkillInput;

export interface PersonalSkillFieldErrors {
  name?: string;
  description?: string;
  guide?: string;
}

// 显式匹配 C0 控制字符与 DEL，确保单行/多行输入校验与后端规则严格对齐。
// eslint-disable-next-line no-control-regex
const INVALID_SINGLE_LINE_CONTROL = /[\u0000-\u001f\u007f]/u;
const INVALID_MULTILINE_CONTROL =
  // eslint-disable-next-line no-control-regex
  /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u;

function codePointLength(value: string): number {
  return countCodePoints(value);
}

export function validateSkillZip(file: File | null): string | null {
  if (!file) {
    return "请选择技能 ZIP 文件。";
  }
  if (!file.name.toLowerCase().endsWith(".zip")) {
    return "技能包必须是 .zip 文件。";
  }
  if (file.size === 0) {
    return "技能包不能为空。";
  }
  if (file.size > SKILL_ZIP_MAX_BYTES) {
    return "技能包不能超过 25 MiB。";
  }
  return null;
}

export function dependencySatisfied(
  check: DependencyCheck | null | undefined,
): boolean {
  return check?.status === "satisfied";
}

export function versionLabel(version: SkillVersion): string {
  return version.label?.trim() || `v${version.ordinal}`;
}

export function skillScope(
  skill: Pick<Skill, "scope">,
): SkillScope {
  // 统一将历史或旧测试夹具中的 "public" 与缺省值归一为 "company"，完整支持三层可见性。
  if (skill.scope === "personal") {
    return "personal";
  }
  if (skill.scope === "department") {
    return "department";
  }
  return "company";
}

export function skillFormat(
  skill: Pick<Skill, "format" | "scope">,
): SkillFormat {
  // 个人技能恒为在线 Markdown；其余公共/部门技能在旧夹具缺省 format 时向后兼容回退为 ZIP 包。
  if (skill.format === "markdown" || skill.format === "package") {
    return skill.format;
  }
  return skillScope(skill) === "personal" ? "markdown" : "package";
}

export function skillExecutionLabel(
  skill: Pick<Skill, "scope" | "script_requires_approval" | "format">,
): string {
  if (
    skillScope(skill) === "personal" ||
    skillFormat(skill) === "markdown"
  ) {
    return "纯文档";
  }
  return skill.script_requires_approval
    ? "脚本执行需审批"
    : "脚本执行无需审批";
}

export function normalizeStarterPrompt(value: string): string | null {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

export function validateStarterPrompt(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  if (codePointLength(trimmed) > SKILL_STARTER_PROMPT_MAX_LENGTH) {
    return `首页默认引导语不能超过 ${SKILL_STARTER_PROMPT_MAX_LENGTH} 个字符。`;
  }
  if (INVALID_MULTILINE_CONTROL.test(trimmed)) {
    return "首页默认引导语不能包含不支持的控制字符。";
  }
  return null;
}

export function validateOptionalAdminSlug(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  if (
    codePointLength(trimmed) > SKILL_SLUG_MAX_LENGTH ||
    !SKILL_SLUG_PATTERN.test(trimmed)
  ) {
    return `技能标识仅允许 1–${SKILL_SLUG_MAX_LENGTH} 位小写字母、数字和连字符，且首尾须为字母或数字。`;
  }
  return null;
}

export function validateVersionLabel(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  if (codePointLength(trimmed) > SKILL_VERSION_LABEL_MAX_LENGTH) {
    return `版本标签不能超过 ${SKILL_VERSION_LABEL_MAX_LENGTH} 个字符。`;
  }
  if (INVALID_SINGLE_LINE_CONTROL.test(trimmed)) {
    return "版本标签不能包含控制字符。";
  }
  return null;
}

export function validatePersonalSkillFile(file: File | null): string | null {
  if (!file) {
    return "请选择 Markdown 文件。";
  }
  if (!file.name.toLowerCase().endsWith(".md")) {
    return "只支持 .md 文件，扩展名不区分大小写。";
  }
  if (file.size === 0) {
    return "Markdown 文件不能为空。";
  }
  if (file.size > PERSONAL_SKILL_FILE_MAX_BYTES) {
    return "Markdown 文件不能超过 1 MiB。";
  }
  return null;
}

export function validatePersonalSkillInput(
  input: PersonalSkillInput,
): PersonalSkillFieldErrors {
  const errors: PersonalSkillFieldErrors = {};
  const name = input.name.trim();
  const description = input.description.trim();
  const guide = input.guide.trim();

  if (!name) {
    errors.name = "请输入技能名称。";
  } else if (codePointLength(name) > PERSONAL_SKILL_NAME_MAX_LENGTH) {
    errors.name = `技能名称不能超过 ${PERSONAL_SKILL_NAME_MAX_LENGTH} 个字符。`;
  } else if (INVALID_SINGLE_LINE_CONTROL.test(name)) {
    errors.name = "技能名称不能包含控制字符。";
  }

  if (!description) {
    errors.description = "请输入技能描述。";
  } else if (
    codePointLength(description) > PERSONAL_SKILL_DESCRIPTION_MAX_LENGTH
  ) {
    errors.description =
      `技能描述不能超过 ${PERSONAL_SKILL_DESCRIPTION_MAX_LENGTH} 个字符。`;
  } else if (INVALID_MULTILINE_CONTROL.test(description)) {
    errors.description = "技能描述不能包含控制字符。";
  }

  if (!guide) {
    errors.guide = "请输入 Markdown 正文。";
  } else if (
    codePointLength(input.guide) > PERSONAL_SKILL_GUIDE_MAX_LENGTH
  ) {
    errors.guide =
      `Markdown 正文不能超过 ${PERSONAL_SKILL_GUIDE_MAX_LENGTH} 个字符。`;
  } else if (INVALID_MULTILINE_CONTROL.test(input.guide)) {
    errors.guide = "Markdown 正文不能包含空字节或其他不支持的控制字符。";
  }

  return errors;
}

export function normalizePersonalSkillInput(
  input: PersonalSkillInput,
): PersonalSkillInput {
  return {
    description: input.description.trim(),
    guide: input.guide,
    name: input.name.trim(),
  };
}
