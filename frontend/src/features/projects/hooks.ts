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
  updateProject,
  type DeleteProjectOptions,
} from "./api";
import type {
  Project,
  ProjectCreateInput,
  ProjectUpdateInput,
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
    queryFn: listAllProjects,
    queryKey: projectKeys.directory(),
  });
}

export function useProject(
  projectId: string | null | undefined,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    enabled: Boolean(projectId) && (options.enabled ?? true),
    queryFn: () => getProject(projectId ?? ""),
    queryKey: projectKeys.detail(projectId ?? ""),
  });
}

export function useCreateProjectMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ProjectCreateInput) => createProject(input),
    onSuccess: async () => {
      await invalidateProjectCaches(queryClient);
    },
  });
}

export function useUpdateProjectMutation(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ProjectUpdateInput) => updateProject(projectId, input),
    onSuccess: async (project: Project) => {
      queryClient.setQueryData(projectKeys.detail(projectId), project);
      await invalidateProjectCaches(queryClient);
    },
  });
}

export function useDeleteProjectMutation(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (options?: DeleteProjectOptions | void) =>
      deleteProject(projectId, options ?? undefined),
    onSuccess: async () => {
      queryClient.removeQueries({
        queryKey: projectKeys.detail(projectId),
      });
      await invalidateProjectCaches(queryClient);
    },
  });
}
