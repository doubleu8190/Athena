import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
} from "../../shared/api/client";
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

export function listProjects(
  query: ProjectListQuery = {},
): Promise<ProjectListResponse> {
  const queryParams: { page?: number; page_size?: number } = {};
  if (query.page !== undefined) {
    queryParams.page = query.page;
  }
  if (query.pageSize !== undefined) {
    queryParams.page_size = query.pageSize;
  }
  return apiGet("/api/v1/projects", {
    params: { query: queryParams },
  });
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
  return apiGet("/api/v1/projects/{project_id}", {
    params: { path: { project_id: projectId } },
  });
}

export function createProject(input: ProjectCreateInput): Promise<Project> {
  return apiPost("/api/v1/projects", {
    body: input,
  });
}

export function updateProject(
  projectId: string,
  input: ProjectUpdateInput,
): Promise<Project> {
  return apiPatch("/api/v1/projects/{project_id}", {
    body: input,
    params: { path: { project_id: projectId } },
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
  return apiDelete("/api/v1/projects/{project_id}", {
    params: {
      path: { project_id: projectId },
      query: options?.detachSessions ? { detach_sessions: true } : undefined,
    },
  });
}
