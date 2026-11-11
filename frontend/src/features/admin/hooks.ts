import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryKey,
} from "@tanstack/react-query";

import {
  authIdentityChangedError,
  getAuthIdentityEpoch,
  isAuthIdentityCurrent,
} from "../../shared/api/client";
import {
  createMcpServer,
  createModelProfile,
  getCompressionPolicy,
  getMcpServer,
  getSystemPrompt,
  getSystemPromptVersion,
  listAdminTools,
  listMcpServers,
  listModelProfiles,
  listSystemPromptVersions,
  restoreSystemPrompt,
  saveSystemPrompt,
  setDefaultModelProfile,
  setModelProfileEnabled,
  updateCompressionPolicy,
  updateMcpServer,
  updateToolApprovalPolicy,
} from "./api";
import type {
  AdminPageQuery,
  AdminTool,
  AdminToolListResponse,
  CompressionPolicyInput,
  McpServerUpdateInput,
  McpServerWriteInput,
  ModelProfile,
  ModelProfileCreateInput,
  SystemPromptKind,
} from "./model";
import { adminKeys } from "./queryKeys";
import { isAdminForbidden } from "./errorMessages";

export function useModelProfiles() {
  return useQuery({
    queryKey: adminKeys.modelProfiles(),
    queryFn: listModelProfiles,
  });
}

export function useCompressionPolicy() {
  return useQuery({
    queryKey: adminKeys.compressionPolicy(),
    queryFn: getCompressionPolicy,
  });
}

export function useSystemPrompt(kind: SystemPromptKind) {
  return useQuery({
    queryKey: adminKeys.systemPrompt(kind),
    queryFn: () => getSystemPrompt(kind),
  });
}

export function useSystemPromptVersions(
  kind: SystemPromptKind,
  query: AdminPageQuery,
) {
  return useQuery({
    queryKey: adminKeys.systemPromptVersions(kind, query),
    queryFn: () => listSystemPromptVersions(kind, query),
  });
}

export function useSystemPromptVersion(
  kind: SystemPromptKind,
  versionId: string | null,
) {
  return useQuery({
    queryKey: adminKeys.systemPromptVersion(kind, versionId ?? ""),
    queryFn: () => getSystemPromptVersion(kind, versionId ?? ""),
    enabled: Boolean(versionId),
    refetchOnMount: "always",
    staleTime: 0,
  });
}

async function refreshSystemPromptQueries(
  queryClient: ReturnType<typeof useQueryClient>,
  kind: SystemPromptKind,
  version: Awaited<ReturnType<typeof saveSystemPrompt>>,
) {
  queryClient.setQueryData(adminKeys.systemPrompt(kind), version);
  await queryClient.invalidateQueries({
    queryKey: adminKeys.systemPromptVersionsAll(kind),
  });
}

export function useSaveSystemPromptMutation(kind: SystemPromptKind) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      content,
      expectedCurrentVersionId,
      identityEpoch,
    }: {
      content: string;
      expectedCurrentVersionId: string;
      identityEpoch: number;
    }) => {
      if (!isAuthIdentityCurrent(identityEpoch)) {
        // 保存提交前再次核对身份，旧页面不能借用新管理员 Cookie 写入提示词。
        throw authIdentityChangedError();
      }
      return saveSystemPrompt(kind, content, expectedCurrentVersionId);
    },
    onSuccess: async (version, variables) => {
      if (!isAuthIdentityCurrent(variables.identityEpoch)) {
        // 响应解析与回调之间也可能发生账号切换，禁止旧版本覆盖新身份缓存。
        return;
      }
      await refreshSystemPromptQueries(queryClient, kind, version);
    },
  });
}

export function useRestoreSystemPromptMutation(kind: SystemPromptKind) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      expectedCurrentVersionId,
      identityEpoch,
      sourceVersionId,
    }: {
      expectedCurrentVersionId: string;
      identityEpoch: number;
      sourceVersionId: string;
    }) => {
      if (!isAuthIdentityCurrent(identityEpoch)) {
        // 恢复同样会立即切换全局配置，跨身份时必须中止而不是复用当前 Cookie。
        throw authIdentityChangedError();
      }
      return restoreSystemPrompt(
        kind,
        sourceVersionId,
        expectedCurrentVersionId,
      );
    },
    onSuccess: async (version, variables) => {
      if (!isAuthIdentityCurrent(variables.identityEpoch)) {
        // 迟到的恢复结果只能属于旧身份，不得污染新身份的管理缓存。
        return;
      }
      await refreshSystemPromptQueries(queryClient, kind, version);
    },
  });
}

export function useCreateModelProfileMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ModelProfileCreateInput) =>
      createModelProfile(input),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: adminKeys.modelProfiles(),
        }),
        queryClient.invalidateQueries({
          queryKey: adminKeys.compressionPolicy(),
        }),
      ]);
    },
  });
}

export function useSetModelProfileEnabledMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      enabled,
      profileId,
    }: {
      enabled: boolean;
      profileId: string;
    }) => setModelProfileEnabled(profileId, enabled),
    onSuccess: async (profile) => {
      queryClient.setQueryData<{
        items: ModelProfile[];
        default_profile_id: string | null;
      }>(adminKeys.modelProfiles(), (current) =>
        current
          ? {
              ...current,
              items: current.items.map((item) =>
                item.id === profile.id ? profile : item,
              ),
            }
          : current,
      );
      await queryClient.invalidateQueries({
        queryKey: adminKeys.compressionPolicy(),
      });
    },
  });
}

export function useSetDefaultModelProfileMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (profileId: string) =>
      setDefaultModelProfile(profileId),
    onSuccess: async (profile) => {
      queryClient.setQueryData<{
        items: ModelProfile[];
        default_profile_id: string | null;
      }>(adminKeys.modelProfiles(), (current) =>
        current
          ? {
              ...current,
              default_profile_id: profile.id,
              items: current.items.map((item) =>
                item.id === profile.id ? profile : item,
              ),
            }
          : current,
      );
    },
  });
}

export function useUpdateCompressionPolicyMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: CompressionPolicyInput) =>
      updateCompressionPolicy(input),
    onSuccess: async (policy) => {
      queryClient.setQueryData(
        adminKeys.compressionPolicy(),
        policy,
      );
    },
  });
}

export function useMcpServers(query: AdminPageQuery) {
  return useQuery({
    queryKey: adminKeys.mcpServers(query),
    queryFn: () => listMcpServers(query),
  });
}

export function useMcpServer(
  serverId: string | undefined,
  enabled: boolean,
) {
  return useQuery({
    queryKey: adminKeys.mcpServer(serverId ?? ""),
    queryFn: () => getMcpServer(serverId ?? ""),
    enabled: Boolean(serverId) && enabled,
    refetchOnMount: "always",
    staleTime: 0,
  });
}

export function useCreateMcpServerMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: McpServerWriteInput) =>
      createMcpServer(input),
    onSuccess: async (server) => {
      queryClient.setQueryData(
        adminKeys.mcpServer(server.id),
        server,
      );
      // 创建会真实发现工具，Server 目录与工具目录必须一起失效。
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: adminKeys.mcpServersAll(),
        }),
        queryClient.invalidateQueries({
          queryKey: adminKeys.toolsAll(),
        }),
      ]);
    },
  });
}

export function useUpdateMcpServerMutation(serverId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: McpServerUpdateInput) =>
      updateMcpServer(serverId, input),
    onSuccess: async (server) => {
      queryClient.setQueryData(
        adminKeys.mcpServer(server.id),
        server,
      );
      // 完整替换会重新发现工具，不能只刷新 Server 详情。
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: adminKeys.mcpServersAll(),
        }),
        queryClient.invalidateQueries({
          queryKey: adminKeys.toolsAll(),
        }),
      ]);
    },
  });
}

export function useAdminTools(query: AdminPageQuery) {
  return useQuery({
    queryKey: adminKeys.tools(query),
    queryFn: () => listAdminTools(query),
  });
}

interface ToolApprovalMutationContext {
  identityEpoch: number;
  previous: Array<[QueryKey, AdminToolListResponse | undefined]>;
}

interface ToolApprovalMutationVariables {
  identityEpoch: number;
  requiresApproval: boolean;
  toolId: string;
}

export function useUpdateToolApprovalMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      identityEpoch,
      requiresApproval,
      toolId,
    }: ToolApprovalMutationVariables) => {
      if (!isAuthIdentityCurrent(identityEpoch)) {
        // 即使 onMutate 刚结束，旧页面也不能用新身份 Cookie 发出审批写入。
        throw authIdentityChangedError();
      }
      return updateToolApprovalPolicy(toolId, requiresApproval);
    },
    onMutate: async ({
      identityEpoch,
      requiresApproval,
      toolId,
    }): Promise<ToolApprovalMutationContext> => {
      // mutation 一旦跨越登录边界，后续成功、失败和失效都不能再碰新用户缓存。
      await queryClient.cancelQueries({
        queryKey: adminKeys.toolsAll(),
      });
      if (!isAuthIdentityCurrent(identityEpoch)) {
        // 在 mutationFn 执行前中止，避免旧页面的操作携带新身份 Cookie 发出。
        throw authIdentityChangedError();
      }
      const previous = queryClient.getQueriesData<AdminToolListResponse>(
        { queryKey: adminKeys.toolsAll() },
      );
      // 先乐观切换当前工具，失败时按 snapshot 回滚，避免界面保留未保存状态。
      queryClient.setQueriesData<AdminToolListResponse>(
        { queryKey: adminKeys.toolsAll() },
        (current) =>
          current
            ? {
                ...current,
                items: current.items.map((tool) =>
                  tool.tool_id === toolId
                    ? {
                        ...tool,
                        requires_approval: requiresApproval,
                      }
                    : tool,
                ),
              }
            : current,
      );
      return { identityEpoch, previous };
    },
    onError: (error, _variables, context) => {
      if (
        !context ||
        !isAuthIdentityCurrent(context.identityEpoch)
      ) {
        return;
      }
      if (isAdminForbidden(error)) {
        // 403 时全局权限处理已删除管理缓存，禁止乐观回滚把旧数据重新写回。
        return;
      }
      context.previous.forEach(([key, data]) => {
        queryClient.setQueryData(key, data);
      });
    },
    onSuccess: (updated, _variables, context) => {
      if (
        !context ||
        !isAuthIdentityCurrent(context.identityEpoch)
      ) {
        return;
      }
      queryClient.setQueriesData<AdminToolListResponse>(
        { queryKey: adminKeys.toolsAll() },
        (current) =>
          current
            ? {
                ...current,
                items: current.items.map((tool) =>
                  tool.tool_id === updated.tool_id ? updated : tool,
                ),
              }
            : current,
      );
    },
    onSettled: async (_data, _error, _variables, context) => {
      if (
        !context ||
        !isAuthIdentityCurrent(context.identityEpoch)
      ) {
        return;
      }
      await queryClient.invalidateQueries({
        queryKey: adminKeys.toolsAll(),
      });
    },
  });
}

export function useUpdateToolApprovalForRow(): {
  error: unknown;
  isPending: boolean;
  pendingToolId: string | null;
  update: (tool: AdminTool) => void;
} {
  const mutation = useUpdateToolApprovalMutation();
  return {
    error: mutation.error,
    isPending: mutation.isPending,
    pendingToolId: mutation.variables?.toolId ?? null,
    update: (tool) => {
      mutation.mutate({
        identityEpoch: getAuthIdentityEpoch(),
        requiresApproval: !tool.requires_approval,
        toolId: tool.tool_id,
      });
    },
  };
}

