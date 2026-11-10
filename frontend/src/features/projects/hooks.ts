import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { sessionKeys } from "../sessions/queryKeys";
import {
  createProject,
  deleteProject,
  getProject,
  listAllProjects,
  listProjects,
  renameProject,
} from "./api";
import type {
  Project,
  ProjectListQuery,
  ProjectNameInput,
} from "./model";
import { projectKeys } from "./queryKeys";

async function invalidateProjectCaches(
  queryClient: ReturnType<typeof useQueryClient>,
): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: projectKeys.all }),
    queryClient.invalidateQueries({ queryKey: sessionKeys.all }),
  ]);
}

export function useProjectDirectory() {
  return useQuery({
    queryKey: projectKeys.directory(),
    queryFn: listAllProjects,
  });
}

export function useProjectList(query: ProjectListQuery = {}) {
  return useQuery({
    queryKey: projectKeys.list(query),
    queryFn: () => listProjects(query),
  });
}

export function useProject(
  projectId: string | null | undefined,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: projectKeys.detail(projectId ?? ""),
    queryFn: () => getProject(projectId ?? ""),
    enabled: Boolean(projectId) && (options.enabled ?? true),
  });
}

export function useCreateProjectMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ProjectNameInput) => createProject(input),
    onSuccess: async () => {
      await invalidateProjectCaches(queryClient);
    },
  });
}

export function useRenameProjectMutation(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ProjectNameInput) =>
      renameProject(projectId, input),
    onSuccess: async (project: Project) => {
      queryClient.setQueryData(projectKeys.detail(projectId), project);
      await invalidateProjectCaches(queryClient);
    },
  });
}

export function useDeleteProjectMutation(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => deleteProject(projectId),
    onSuccess: async () => {
      queryClient.removeQueries({
        queryKey: projectKeys.detail(projectId),
      });
      await invalidateProjectCaches(queryClient);
    },
  });
}

