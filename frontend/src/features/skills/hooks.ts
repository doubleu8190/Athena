import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { adminKeys } from "../admin/queryKeys";
import {
  getAdminSkill,
  getAdminSkillVersion,
  importSkillPackage,
  listAdminSkills,
  listAdminSkillVersions,
  listPublicSkills,
  updateSkillConfiguration,
  uploadSkillVersion,
} from "./api";
import type { SkillConfigurationInput } from "./model";
import { skillKeys } from "./queryKeys";

async function refreshSkillCatalogs(
  queryClient: ReturnType<typeof useQueryClient>,
  skillId?: string,
) {
  await Promise.all([
    queryClient.invalidateQueries({
      queryKey: adminKeys.skillsAll(),
    }),
    queryClient.invalidateQueries({
      queryKey: skillKeys.catalog(),
    }),
    skillId
      ? queryClient.invalidateQueries({
          queryKey: adminKeys.skill(skillId),
        })
      : Promise.resolve(),
    skillId
      ? queryClient.invalidateQueries({
          queryKey: adminKeys.skillVersionsAll(skillId),
        })
      : Promise.resolve(),
  ]);
}

export function usePublicSkills(enabled = true) {
  return useQuery({
    enabled,
    queryFn: listPublicSkills,
    queryKey: skillKeys.catalog(),
  });
}

export function useAdminSkills() {
  return useQuery({
    queryFn: listAdminSkills,
    queryKey: adminKeys.skills(),
  });
}

export function useAdminSkill(skillId: string | null) {
  return useQuery({
    enabled: Boolean(skillId),
    queryFn: () => getAdminSkill(skillId ?? ""),
    queryKey: adminKeys.skill(skillId ?? ""),
    refetchOnMount: "always",
    staleTime: 0,
  });
}

export function useAdminSkillVersions(
  skillId: string | null,
  page: number,
  pageSize: number,
) {
  return useQuery({
    enabled: Boolean(skillId),
    queryFn: () =>
      listAdminSkillVersions(skillId ?? "", page, pageSize),
    queryKey: adminKeys.skillVersions(
      skillId ?? "",
      page,
      pageSize,
    ),
    staleTime: 0,
  });
}

export function useAdminSkillVersion(
  skillId: string | null,
  versionId: string | null,
) {
  return useQuery({
    enabled: Boolean(skillId && versionId),
    queryFn: () =>
      getAdminSkillVersion(skillId ?? "", versionId ?? ""),
    queryKey: adminKeys.skillVersion(
      skillId ?? "",
      versionId ?? "",
    ),
    staleTime: 0,
  });
}

export function useImportSkillMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      file,
      requestId,
    }: {
      file: File;
      requestId: string;
    }) => importSkillPackage(file, requestId),
    onSuccess: async (details) => {
      queryClient.setQueryData(
        adminKeys.skill(details.id),
        details,
      );
      await refreshSkillCatalogs(queryClient, details.id);
    },
  });
}

export function useUploadSkillVersionMutation(skillId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      file,
      label,
      requestId,
    }: {
      file: File;
      label?: string;
      requestId: string;
    }) => uploadSkillVersion(skillId, file, requestId, label),
    onSuccess: async () => {
      await refreshSkillCatalogs(queryClient, skillId);
    },
  });
}

export function useUpdateSkillConfigurationMutation(skillId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: SkillConfigurationInput) =>
      updateSkillConfiguration(skillId, input),
    onSuccess: async (details) => {
      // PATCH 返回同一修订下的完整详情，先写入缓存可避免保存成功到后台刷新间短暂显示旧草稿。
      queryClient.setQueryData(adminKeys.skill(skillId), details);
      await refreshSkillCatalogs(queryClient, skillId);
    },
  });
}
