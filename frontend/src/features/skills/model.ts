import type { ApiSchemas } from "../../shared/api/client";
import { countCodePoints } from "../../shared/lib/text";
import type { VisibilityGrantInput } from "../knowledge/model";

export const SKILL_ZIP_MAX_BYTES = 25 * 1024 * 1024;
export const PERSONAL_SKILL_FILE_MAX_BYTES = 1024 * 1024;
export const PERSONAL_SKILL_NAME_MAX_LENGTH = 120;
export const PERSONAL_SKILL_DESCRIPTION_MAX_LENGTH = 1000;
export const PERSONAL_SKILL_GUIDE_MAX_LENGTH = 100_000;
export const SKILL_STARTER_PROMPT_MAX_LENGTH = 500;
export const SKILL_SLUG_MAX_LENGTH = 64;
export const SKILL_VERSION_LABEL_MAX_LENGTH = 64;
export const SKILL_SLUG_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;

// 直连 OpenAPI 技能管理与个人技能契约。
export type Skill = ApiSchemas["SkillSummaryResponse"];
export type SkillFormat = Skill["format"];
export type SkillScope = Skill["scope"];
export type DependencyCheckStatus = ApiSchemas["DependencyCheckStatus"];
export type DependencyCheckItem = ApiSchemas["SkillDependencyItemResponse"];
export type DependencyCheck = ApiSchemas["SkillDependencyCheckResponse"];
export type SkillScriptDefinition = ApiSchemas["SkillScriptResponse"];
export type SkillPackageResource = ApiSchemas["SkillPackageResourceResponse"];
export type SkillVersion = ApiSchemas["SkillVersionResponse"];
export type SkillVersionPage = ApiSchemas["SkillVersionListResponse"];
export type SkillListResponse = ApiSchemas["SkillDirectoryResponse"];
export type SkillAdminDetails = ApiSchemas["SkillAdminDetailsResponse"];
export type SkillConfigurationInput = ApiSchemas["SkillConfigurationRequest"];
export type AdminSkillMarkdownCreateInput =
  ApiSchemas["AdminSkillMarkdownCreateRequest"];
export type AdminSkillMarkdownVersionInput =
  ApiSchemas["AdminSkillMarkdownVersionRequest"];
export type PersonalSkillInput = ApiSchemas["SkillPersonalCreateRequest"];
export type PersonalSkillDraft = ApiSchemas["SkillMarkdownParseResponse"];

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
  return skill.scope;
}

export function skillFormat(
  skill: Pick<Skill, "format">,
): SkillFormat {
  return skill.format;
}

export function skillExecutionLabel(
  skill: Pick<Skill, "scope" | "script_requires_approval" | "format">,
): string {
  if (skill.scope === "personal" || skill.format === "markdown") {
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
