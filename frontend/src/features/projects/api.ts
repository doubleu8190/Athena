import { apiRequest } from "../../shared/api/client";
import type {
  Project,
  ProjectListQuery,
  ProjectListResponse,
  ProjectNameInput,
} from "./model";

function toProjectListQuery(query: ProjectListQuery): string {
  const params = new URLSearchParams();
  if (query.page !== undefined) {
    params.set("page", String(query.page));
  }
  if (query.pageSize !== undefined) {
    params.set("page_size", String(query.pageSize));
  }
  const serialized = params.toString();
  return serialized ? `?${serialized}` : "";
}

export function listProjects(
  query: ProjectListQuery = {},
): Promise<ProjectListResponse> {
  return apiRequest<ProjectListResponse>(
    `/projects${toProjectListQuery(query)}`,
    { method: "GET" },
  );
}

export async function listAllProjects(): Promise<Project[]> {
  const pageSize = 100;
  const projects: Project[] = [];
  let page = 1;
  let total = 0;

  do {
    const result = await listProjects({ page, pageSize });
    projects.push(...result.items);
    total = result.total;
    if (result.items.length === 0) {
      break;
    }
    page += 1;
  } while (projects.length < total);

  return projects;
}

export function getProject(projectId: string): Promise<Project> {
  return apiRequest<Project>(`/projects/${encodeURIComponent(projectId)}`, {
    method: "GET",
  });
}

export function createProject(input: ProjectNameInput): Promise<Project> {
  return apiRequest<Project>("/projects", {
    method: "POST",
    body: input,
  });
}

export function renameProject(
  projectId: string,
  input: ProjectNameInput,
): Promise<Project> {
  return apiRequest<Project>(`/projects/${encodeURIComponent(projectId)}`, {
    method: "PATCH",
    body: input,
  });
}

export function deleteProject(projectId: string): Promise<void> {
  return apiRequest<void>(`/projects/${encodeURIComponent(projectId)}`, {
    method: "DELETE",
  });
}

