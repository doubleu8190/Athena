import { apiMultipart, apiRequest } from "../../shared/api/client";
import type {
  Skill,
  SkillAdminDetails,
  SkillConfigurationInput,
  SkillListResponse,
  SkillVersion,
  SkillVersionPage,
} from "./model";

function adminSkillPath(skillId: string): string {
  return `/admin/skills/${encodeURIComponent(skillId)}`;
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
): Promise<SkillAdminDetails> {
  const form = new FormData();
  form.set("package", file);
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
