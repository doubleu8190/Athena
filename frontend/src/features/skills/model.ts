export const SKILL_ZIP_MAX_BYTES = 25 * 1024 * 1024;
export const PERSONAL_SKILL_FILE_MAX_BYTES = 1024 * 1024;
export const PERSONAL_SKILL_NAME_MAX_LENGTH = 120;
export const PERSONAL_SKILL_DESCRIPTION_MAX_LENGTH = 1000;
export const PERSONAL_SKILL_GUIDE_MAX_LENGTH = 100_000;

export type SkillScope = "public" | "personal";

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
  scope?: SkillScope;
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
  script_requires_approval: boolean;
}

export interface SkillFormState {
  currentVersionId: string;
  enabled: boolean;
  scriptRequiresApproval: boolean;
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

const INVALID_SINGLE_LINE_CONTROL = /[\u0000-\u001f\u007f]/u;
const INVALID_MULTILINE_CONTROL =
  /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u;

function codePointLength(value: string): number {
  return Array.from(value).length;
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

export function skillScope(skill: Pick<Skill, "scope">): SkillScope {
  // 旧目录 fixture 和旧快照没有 scope 时继续按公共技能解释，保持向后兼容。
  return skill.scope === "personal" ? "personal" : "public";
}

export function skillExecutionLabel(
  skill: Pick<Skill, "scope" | "script_requires_approval">,
): string {
  if (skillScope(skill) === "personal") {
    return "纯文档";
  }
  return skill.script_requires_approval
    ? "脚本执行需审批"
    : "脚本执行无需审批";
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
