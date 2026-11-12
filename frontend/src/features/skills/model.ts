export const SKILL_ZIP_MAX_BYTES = 25 * 1024 * 1024;

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
