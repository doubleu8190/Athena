import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { adminKeys } from "../admin/queryKeys";
import {
  createAdminMarkdownSkill,
  createPersonalSkill,
  deleteAdminSkill,
  deletePersonalSkill,
  getAdminSkill,
  getAdminSkillVersion,
  getPersonalSkill,
  importSkillPackage,
  listAdminSkills,
  listAdminSkillVersions,
  listPersonalSkills,
  listPublicSkills,
  parseAdminSkillMarkdown,
  parsePersonalSkillFile,
  publishAdminMarkdownVersion,
  savePersonalSkillVersion,
  setPersonalSkillEnabled,
  updateSkillConfiguration,
  uploadSkillVersion,
} from "./api";
import type {
  AdminSkillMarkdownCreateInput,
  AdminSkillMarkdownVersionInput,
  ImportSkillPackageOptions,
  PersonalSkillInput,
  SkillConfigurationInput,
} from "./model";
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
      queryKey: skillKeys.adminList(),
    }),
    queryClient.invalidateQueries({
      queryKey: skillKeys.publicList(),
    }),
    skillId
      ? queryClient.invalidateQueries({
          queryKey: skillKeys.adminDetail(skillId),
        })
      : Promise.resolve(),
    skillId
      ? queryClient.invalidateQueries({
          queryKey: skillKeys.adminVersions(skillId),
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
      ownerDepartmentId,
      requestId,
      scope,
      visibilityGrants,
    }: ImportSkillPackageOptions & {
      file: File;
      requestId: string;
    }) =>
      importSkillPackage(file, requestId, {
        ownerDepartmentId,
        scope,
        visibilityGrants,
      }),
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

export function useParseAdminSkillMarkdownMutation() {
  return useMutation({
    mutationFn: parseAdminSkillMarkdown,
  });
}

export function useCreateAdminMarkdownSkillMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      input,
      requestId,
    }: {
      input: AdminSkillMarkdownCreateInput;
      requestId: string;
    }) => createAdminMarkdownSkill(input, requestId),
    onSuccess: async (details) => {
      queryClient.setQueryData(skillKeys.adminDetail(details.id), details);
      await refreshSkillCatalogs(queryClient, details.id);
    },
  });
}

export function usePublishAdminMarkdownVersionMutation(skillId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      input,
      requestId,
    }: {
      input: AdminSkillMarkdownVersionInput;
      requestId: string;
    }) => publishAdminMarkdownVersion(skillId, input, requestId),
    onSuccess: async (details) => {
      queryClient.setQueryData(skillKeys.adminDetail(details.id), details);
      await refreshSkillCatalogs(queryClient, details.id);
    },
  });
}

/** 软删除部门级技能并清理缓存与目录。 */
export function useDeleteAdminSkillMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      expectedRevision,
      skillId,
    }: {
      expectedRevision: number;
      skillId: string;
    }) => deleteAdminSkill(skillId, expectedRevision),
    onSuccess: async (_result, variables) => {
      queryClient.removeQueries({
        queryKey: adminKeys.skill(variables.skillId),
      });
      await refreshSkillCatalogs(queryClient);
    },
  });
}

async function refreshPersonalSkillData(
  queryClient: ReturnType<typeof useQueryClient>,
  skillId?: string,
) {
  await Promise.all([
    queryClient.invalidateQueries({
      queryKey: skillKeys.personalList(),
    }),
    queryClient.invalidateQueries({
      queryKey: skillKeys.catalog(),
    }),
    skillId
      ? queryClient.invalidateQueries({
          queryKey: skillKeys.personalDetail(skillId),
        })
      : Promise.resolve(),
  ]);
}

export function usePersonalSkills(enabled = true) {
  return useQuery({
    enabled,
    queryFn: listPersonalSkills,
    queryKey: skillKeys.personalList(),
  });
}

export function usePersonalSkill(skillId: string | null) {
  return useQuery({
    enabled: Boolean(skillId),
    queryFn: () => getPersonalSkill(skillId ?? ""),
    queryKey: skillKeys.personalDetail(skillId ?? ""),
    staleTime: 0,
  });
}

export function useParsePersonalSkillMutation() {
  return useMutation({
    mutationFn: parsePersonalSkillFile,
  });
}

export function useCreatePersonalSkillMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      input,
      requestId,
    }: {
      input: PersonalSkillInput;
      requestId: string;
    }) => createPersonalSkill(input, requestId),
    onSuccess: async (details) => {
      queryClient.setQueryData(
        skillKeys.personalDetail(details.id),
        details,
      );
      // 创建只启用服务端目录，不把新技能写入当前聊天选择，避免隐式扩大下一轮 Run 范围。
      await refreshPersonalSkillData(queryClient, details.id);
    },
  });
}

export function useSavePersonalSkillVersionMutation(skillId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      expectedRevision,
      input,
      requestId,
    }: {
      expectedRevision: number;
      input: PersonalSkillInput;
      requestId: string;
    }) =>
      savePersonalSkillVersion(
        skillId,
        { ...input, expected_revision: expectedRevision },
        requestId,
      ),
    onSuccess: async (details) => {
      queryClient.setQueryData(
        skillKeys.personalDetail(skillId),
        details,
      );
      await refreshPersonalSkillData(queryClient, skillId);
    },
  });
}

export function useSetPersonalSkillEnabledMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      enabled,
      expectedRevision,
      skillId,
    }: {
      enabled: boolean;
      expectedRevision: number;
      skillId: string;
    }) => setPersonalSkillEnabled(skillId, enabled, expectedRevision),
    onSuccess: async (details) => {
      queryClient.setQueryData(
        skillKeys.personalDetail(details.id),
        details,
      );
      await refreshPersonalSkillData(queryClient, details.id);
    },
  });
}

export function useDeletePersonalSkillMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      expectedRevision,
      skillId,
    }: {
      expectedRevision: number;
      skillId: string;
    }) => deletePersonalSkill(skillId, expectedRevision),
    onSuccess: async (_result, variables) => {
      queryClient.removeQueries({
        queryKey: skillKeys.personalDetail(variables.skillId),
      });
      await refreshPersonalSkillData(queryClient);
    },
  });
}
