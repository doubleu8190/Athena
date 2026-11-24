import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import {
  authIdentityChangedError,
  getAuthIdentityEpoch,
  isAuthIdentityCurrent,
} from "../../shared/api/client";
import {
  createUserMemory,
  deleteUserMemory,
  getUserPreferences,
  listUserMemories,
  updateUserMemory,
  updateUserPreferences,
} from "./api";
import { isMemoryLimitReachedError } from "./errorMessages";
import type {
  CreateUserMemoryInput,
  UpdateUserMemoryInput,
  UpdateUserWorkProfileInput,
  UserMemory,
  UserMemoryListResponse,
  UserWorkProfile,
} from "./model";
import { preferenceKeys } from "./queryKeys";

export function useUserPreferences() {
  return useQuery({
    queryFn: getUserPreferences,
    queryKey: preferenceKeys.profile(),
  });
}

interface UpdatePreferencesContext {
  identityEpoch: number;
  previousProfile: UserWorkProfile | undefined;
}

/**
 * 更新个人工作偏好与显式记忆开关：
 * 先在缓存中执行乐观更新以保证 Switch 切换零延迟响应，失败时按快照精确回滚；
 * 跨身份切换时通过 identityEpoch 阻断旧身份回写或失效新用户缓存。
 */
export function useUpdateUserPreferencesMutation() {
  const queryClient = useQueryClient();
  return useMutation<
    UserWorkProfile,
    unknown,
    UpdateUserWorkProfileInput,
    UpdatePreferencesContext
  >({
    mutationFn: (input) => updateUserPreferences(input),
    onMutate: async (input) => {
      const identityEpoch = getAuthIdentityEpoch();
      await queryClient.cancelQueries({ queryKey: preferenceKeys.profile() });
      if (!isAuthIdentityCurrent(identityEpoch)) {
        throw authIdentityChangedError();
      }
      const previousProfile = queryClient.getQueryData<UserWorkProfile>(
        preferenceKeys.profile(),
      );
      queryClient.setQueryData<UserWorkProfile>(preferenceKeys.profile(), {
        custom_instructions: input.custom_instructions,
        memory_enabled: input.memory_enabled,
        updated_at: previousProfile?.updated_at ?? null,
      });
      return { identityEpoch, previousProfile };
    },
    onError: (_error, _input, context) => {
      if (!context || !isAuthIdentityCurrent(context.identityEpoch)) {
        return;
      }
      queryClient.setQueryData(
        preferenceKeys.profile(),
        context.previousProfile,
      );
    },
    onSuccess: (saved, _input, context) => {
      if (!context || !isAuthIdentityCurrent(context.identityEpoch)) {
        return;
      }
      queryClient.setQueryData(preferenceKeys.profile(), saved);
    },
    onSettled: async (_data, _error, _input, context) => {
      if (!context || !isAuthIdentityCurrent(context.identityEpoch)) {
        return;
      }
      await queryClient.invalidateQueries({
        queryKey: preferenceKeys.profile(),
      });
    },
  });
}

export function useUserMemories() {
  return useQuery({
    queryFn: listUserMemories,
    queryKey: preferenceKeys.memories(),
  });
}

export function useCreateUserMemoryMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateUserMemoryInput) => createUserMemory(input),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: preferenceKeys.memories(),
      });
    },
    onError: async (error) => {
      // 并发触发 409 配额上限时主动刷新记忆列表，让配额条与服务端真实总数对齐。
      if (isMemoryLimitReachedError(error)) {
        await queryClient.invalidateQueries({
          queryKey: preferenceKeys.memories(),
        });
      }
    },
  });
}

export function useUpdateUserMemoryMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      input,
      memoryId,
    }: {
      input: UpdateUserMemoryInput;
      memoryId: string;
    }) => updateUserMemory(memoryId, input),
    onSuccess: async (updated: UserMemory) => {
      queryClient.setQueryData<UserMemoryListResponse>(
        preferenceKeys.memories(),
        (current) =>
          current
            ? {
                ...current,
                items: current.items.map((item) =>
                  item.id === updated.id ? updated : item,
                ),
              }
            : current,
      );
      await queryClient.invalidateQueries({
        queryKey: preferenceKeys.memories(),
      });
    },
  });
}

export function useDeleteUserMemoryMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (memoryId: string) => deleteUserMemory(memoryId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: preferenceKeys.memories(),
      });
    },
  });
}
