import {
  apiDelete,
  apiGet,
  apiMultipart,
  apiPatch,
  apiPost,
  type ApiSchemas,
} from "../../shared/api/client";
import type {
  AdminSkillMarkdownCreateInput,
  AdminSkillMarkdownVersionInput,
  DependencyCheck,
  DependencyCheckStatus,
  ImportSkillPackageOptions,
  PersonalSkillDraft,
  PersonalSkillInput,
  Skill,
  SkillAdminDetails,
  SkillConfigurationInput,
  SkillVersion,
  SkillVersionPage,
} from "./model";

function adminSkillPath(skillId: string): string {
  return `/admin/skills/${encodeURIComponent(skillId)}`;
}

function toDependencyCheckStatus(value: string): DependencyCheckStatus {
  if (
    value === "satisfied" ||
    value === "dependency_unsatisfied" ||
    value === "dependency_invalid"
  ) {
    return value;
  }
  return "dependency_invalid";
}

function toDependencyCheck(
  raw: ApiSchemas["SkillDependencyCheckResponse"],
): DependencyCheck {
  return {
    ...raw,
    status: toDependencyCheckStatus(raw.status),
  };
}

function toSkillVersion(
  raw: ApiSchemas["SkillVersionResponse"],
): SkillVersion {
  return {
    ...raw,
    dependency_check: toDependencyCheck(raw.dependency_check),
  };
}

function toSkill(raw: ApiSchemas["SkillSummaryResponse"]): Skill {
  return {
    ...raw,
    dependency_check: raw.dependency_check
      ? toDependencyCheck(raw.dependency_check)
      : null,
  };
}

function toSkillAdminDetails(
  raw: ApiSchemas["SkillAdminDetailsResponse"],
): SkillAdminDetails {
  return {
    ...raw,
    dependency_check: raw.dependency_check
      ? toDependencyCheck(raw.dependency_check)
      : null,
    versions: raw.versions.map(toSkillVersion),
  };
}

export async function listPublicSkills(): Promise<Skill[]> {
  const response = await apiGet("/api/v1/skills");
  return response.items.map(toSkill);
}

export async function listAdminSkills(): Promise<Skill[]> {
  const response = await apiGet("/api/v1/admin/skills");
  return response.items.map(toSkill);
}

export async function getAdminSkill(
  skillId: string,
): Promise<SkillAdminDetails> {
  return toSkillAdminDetails(
    await apiGet("/api/v1/admin/skills/{skill_id}", {
      params: { path: { skill_id: skillId } },
    }),
  );
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
  const response = await apiMultipart<ApiSchemas["SkillAdminDetailsResponse"]>(
    "/admin/skills/import",
    form,
    {
      method: "POST",
      headers: { "Idempotency-Key": requestId },
    },
  );
  return toSkillAdminDetails(response);
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
  const response = await apiMultipart<ApiSchemas["SkillVersionResponse"]>(
    `${adminSkillPath(skillId)}/versions${suffix}`,
    form,
    {
      method: "POST",
      headers: { "Idempotency-Key": requestId },
    },
  );
  return toSkillVersion(response);
}

export async function listAdminSkillVersions(
  skillId: string,
  page: number,
  pageSize: number,
): Promise<SkillVersionPage> {
  const response = await apiGet("/api/v1/admin/skills/{skill_id}/versions", {
    params: {
      path: { skill_id: skillId },
      query: { page, page_size: pageSize },
    },
  });
  return {
    ...response,
    items: response.items.map(toSkillVersion),
  };
}

export async function getAdminSkillVersion(
  skillId: string,
  versionId: string,
): Promise<SkillVersion> {
  return toSkillVersion(
    await apiGet("/api/v1/admin/skills/{skill_id}/versions/{version_id}", {
      params: { path: { skill_id: skillId, version_id: versionId } },
    }),
  );
}

export async function updateSkillConfiguration(
  skillId: string,
  input: SkillConfigurationInput,
): Promise<SkillAdminDetails> {
  return toSkillAdminDetails(
    await apiPatch("/api/v1/admin/skills/{skill_id}", {
      body: input,
      params: { path: { skill_id: skillId } },
    }),
  );
}

/** 契约 3.4：解析管理端 .md 技能草稿（纯解析不落库）。 */
export async function parseAdminSkillMarkdown(
  file: File,
): Promise<PersonalSkillDraft> {
  const form = new FormData();
  form.set("file", file);
  return apiMultipart<ApiSchemas["SkillMarkdownParseResponse"]>(
    "/admin/skills/parse-markdown",
    form,
    { method: "POST" },
  );
}

/** 契约 3.5：在线创建公共或部门 Markdown 模板技能（须携带 Idempotency-Key）。 */
export async function createAdminMarkdownSkill(
  input: AdminSkillMarkdownCreateInput,
  requestId: string,
): Promise<SkillAdminDetails> {
  return toSkillAdminDetails(
    await apiPost("/api/v1/admin/skills/markdown", {
      body: input,
      params: { header: { "Idempotency-Key": requestId } },
    }),
  );
}

/** 契约 3.6：为已有 format="markdown" 技能在线发布不可变新版本（须携带 Idempotency-Key）。 */
export async function publishAdminMarkdownVersion(
  skillId: string,
  input: AdminSkillMarkdownVersionInput,
  requestId: string,
): Promise<SkillAdminDetails> {
  return toSkillAdminDetails(
    await apiPost("/api/v1/admin/skills/{skill_id}/versions/markdown", {
      body: input,
      params: {
        header: { "Idempotency-Key": requestId },
        path: { skill_id: skillId },
      },
    }),
  );
}

/** 软删除部门级技能（后端严禁删除 company 级技能，须携带乐观锁 expected_revision）。 */
export async function deleteAdminSkill(
  skillId: string,
  expectedRevision: number,
): Promise<void> {
  await apiDelete("/api/v1/admin/skills/{skill_id}", {
    params: {
      path: { skill_id: skillId },
      query: { expected_revision: expectedRevision },
    },
  });
}

export async function listPersonalSkills(): Promise<Skill[]> {
  const response = await apiGet("/api/v1/me/skills");
  return response.items.map(toSkill);
}

export async function getPersonalSkill(
  skillId: string,
): Promise<SkillAdminDetails> {
  return toSkillAdminDetails(
    await apiGet("/api/v1/me/skills/{skill_id}", {
      params: { path: { skill_id: skillId } },
    }),
  );
}

export async function parsePersonalSkillFile(
  file: File,
): Promise<PersonalSkillDraft> {
  const form = new FormData();
  form.set("file", file);
  return apiMultipart<ApiSchemas["SkillMarkdownParseResponse"]>(
    "/me/skills/parse",
    form,
    { method: "POST" },
  );
}

export async function createPersonalSkill(
  input: PersonalSkillInput,
  requestId: string,
): Promise<SkillAdminDetails> {
  return toSkillAdminDetails(
    await apiPost("/api/v1/me/skills", {
      body: input,
      params: { header: { "Idempotency-Key": requestId } },
    }),
  );
}

export async function savePersonalSkillVersion(
  skillId: string,
  input: PersonalSkillInput & { expected_revision: number },
  requestId: string,
): Promise<SkillAdminDetails> {
  return toSkillAdminDetails(
    await apiPost("/api/v1/me/skills/{skill_id}/versions", {
      body: input,
      params: {
        header: { "Idempotency-Key": requestId },
        path: { skill_id: skillId },
      },
    }),
  );
}

export async function setPersonalSkillEnabled(
  skillId: string,
  enabled: boolean,
  expectedRevision: number,
): Promise<SkillAdminDetails> {
  return toSkillAdminDetails(
    await apiPatch("/api/v1/me/skills/{skill_id}", {
      body: {
        enabled,
        expected_revision: expectedRevision,
      },
      params: { path: { skill_id: skillId } },
    }),
  );
}

export async function deletePersonalSkill(
  skillId: string,
  expectedRevision: number,
): Promise<void> {
  await apiDelete("/api/v1/me/skills/{skill_id}", {
    params: {
      path: { skill_id: skillId },
      query: { expected_revision: expectedRevision },
    },
  });
}
