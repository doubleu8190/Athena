import { apiMultipart, apiRequest } from "../../shared/api/client";
import type {
  ImportSkillPackageOptions,
  Skill,
  SkillAdminDetails,
  SkillConfigurationInput,
  SkillListResponse,
  PersonalSkillDraft,
  PersonalSkillInput,
  SkillVersion,
  SkillVersionPage,
} from "./model";

function adminSkillPath(skillId: string): string {
  return `/admin/skills/${encodeURIComponent(skillId)}`;
}

function personalSkillPath(skillId: string): string {
  return `/me/skills/${encodeURIComponent(skillId)}`;
}

export async function listPublicSkills(): Promise<Skill[]> {
  const response = await apiRequest<SkillListResponse>("/skills", {
    method: "GET",
  });
  return response.items;
}

export async function listAdminSkills(): Promise<Skill[]> {
  const response = await apiRequest<SkillListResponse>(
    "/admin/skills",
    { method: "GET" },
  );
  return response.items;
}

export async function getAdminSkill(
  skillId: string,
): Promise<SkillAdminDetails> {
  return apiRequest<SkillAdminDetails>(adminSkillPath(skillId), {
    method: "GET",
  });
}

export async function importSkillPackage(
  file: File,
  requestId: string,
  options?: ImportSkillPackageOptions,
): Promise<SkillAdminDetails> {
  const form = new FormData();
  form.set("package", file);
  if (options?.scope) {
    form.set("scope", options.scope);
  }
  if (options?.scope === "department" && options.ownerDepartmentId?.trim()) {
    form.set("owner_department_id", options.ownerDepartmentId.trim());
  }
  if (options?.visibilityGrants && options.visibilityGrants.length > 0) {
    form.set("visibility_grants", JSON.stringify(options.visibilityGrants));
  }
  return apiMultipart<SkillAdminDetails>("/admin/skills/import", form, {
    method: "POST",
    headers: { "Idempotency-Key": requestId },
  });
}

export async function uploadSkillVersion(
  skillId: string,
  file: File,
  requestId: string,
  label?: string,
): Promise<SkillVersion> {
  const form = new FormData();
  form.set("package", file);
  const params = new URLSearchParams();
  if (label?.trim()) {
    params.set("label", label.trim());
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return apiMultipart<SkillVersion>(
    `${adminSkillPath(skillId)}/versions${suffix}`,
    form,
    {
      method: "POST",
      headers: { "Idempotency-Key": requestId },
    },
  );
}

export function listAdminSkillVersions(
  skillId: string,
  page: number,
  pageSize: number,
): Promise<SkillVersionPage> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  return apiRequest<SkillVersionPage>(
    `${adminSkillPath(skillId)}/versions?${params.toString()}`,
    { method: "GET" },
  );
}

export function getAdminSkillVersion(
  skillId: string,
  versionId: string,
): Promise<SkillVersion> {
  return apiRequest<SkillVersion>(
    `${adminSkillPath(skillId)}/versions/${encodeURIComponent(versionId)}`,
    { method: "GET" },
  );
}

export function updateSkillConfiguration(
  skillId: string,
  input: SkillConfigurationInput,
): Promise<SkillAdminDetails> {
  return apiRequest<SkillAdminDetails>(adminSkillPath(skillId), {
    method: "PATCH",
    body: input,
  });
}

/** 软删除部门级技能（后端严禁删除 company 级技能，须携带乐观锁 expected_revision）。 */
export async function deleteAdminSkill(
  skillId: string,
  expectedRevision: number,
): Promise<void> {
  await apiRequest<void>(
    `${adminSkillPath(skillId)}?expected_revision=${expectedRevision}`,
    { method: "DELETE" },
  );
}

export async function listPersonalSkills(): Promise<Skill[]> {
  const response = await apiRequest<SkillListResponse>("/me/skills", {
    method: "GET",
  });
  return response.items;
}

export function getPersonalSkill(
  skillId: string,
): Promise<SkillAdminDetails> {
  return apiRequest<SkillAdminDetails>(personalSkillPath(skillId), {
    method: "GET",
  });
}

export async function parsePersonalSkillFile(
  file: File,
): Promise<PersonalSkillDraft> {
  const form = new FormData();
  form.set("file", file);
  return apiMultipart<PersonalSkillDraft>("/me/skills/parse", form, {
    method: "POST",
  });
}

export function createPersonalSkill(
  input: PersonalSkillInput,
  requestId: string,
): Promise<SkillAdminDetails> {
  return apiRequest<SkillAdminDetails>("/me/skills", {
    method: "POST",
    body: input,
    headers: { "Idempotency-Key": requestId },
  });
}

export function savePersonalSkillVersion(
  skillId: string,
  input: PersonalSkillInput & { expected_revision: number },
  requestId: string,
): Promise<SkillAdminDetails> {
  return apiRequest<SkillAdminDetails>(
    `${personalSkillPath(skillId)}/versions`,
    {
      method: "POST",
      body: input,
      headers: { "Idempotency-Key": requestId },
    },
  );
}

export function setPersonalSkillEnabled(
  skillId: string,
  enabled: boolean,
  expectedRevision: number,
): Promise<SkillAdminDetails> {
  return apiRequest<SkillAdminDetails>(personalSkillPath(skillId), {
    method: "PATCH",
    body: {
      enabled,
      expected_revision: expectedRevision,
    },
  });
}

export async function deletePersonalSkill(
  skillId: string,
  expectedRevision: number,
): Promise<void> {
  await apiRequest<void>(
    `${personalSkillPath(skillId)}?expected_revision=${expectedRevision}`,
    { method: "DELETE" },
  );
}
