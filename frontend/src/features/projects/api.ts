import { apiRequest } from "../../shared/api/client";
import type {
  Project,
  ProjectCreateInput,
  ProjectListQuery,
  ProjectListResponse,
  ProjectNameInput,
  ProjectUpdateInput,
} from "./model";

export interface DeleteProjectOptions {
  /** 为 true 时在同事务内将项目内闲置会话解绑为无项目会话后再软删项目。 */
  detachSessions?: boolean;
}

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

export function createProject(input: ProjectCreateInput): Promise<Project> {
  return apiRequest<Project>("/projects", {
    body: input,
    method: "POST",
  });
}

export function updateProject(
  projectId: string,
  input: ProjectUpdateInput,
): Promise<Project> {
  return apiRequest<Project>(`/projects/${encodeURIComponent(projectId)}`, {
    body: input,
    method: "PATCH",
  });
}

/** 保留旧导出别名以兼容仅改名调用方。 */
export function renameProject(
  projectId: string,
  input: ProjectNameInput,
): Promise<Project> {
  return updateProject(projectId, input);
}

export function deleteProject(
  projectId: string,
  options?: DeleteProjectOptions,
): Promise<void> {
  const query = options?.detachSessions ? "?detach_sessions=true" : "";
  return apiRequest<void>(
    `/projects/${encodeURIComponent(projectId)}${query}`,
    {
      method: "DELETE",
    },
  );
}
