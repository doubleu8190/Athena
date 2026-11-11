import { useMutation, useQueryClient } from "@tanstack/react-query";

import { changePassword, login, logout, register, updateCurrentUser } from "./api";
import {
  clearAuthenticatedIdentity,
  refreshAuthenticatedUser,
  setAuthenticatedIdentity,
} from "./cacheBoundary";
import type { LoginInput, RegisterInput } from "./model";

export function useLoginMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: LoginInput) => login(input),
    onSuccess: (user) => {
      setAuthenticatedIdentity(queryClient, user);
    },
  });
}

export function useUpdateCurrentUserMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (displayName: string) => {
      await updateCurrentUser(displayName);
      return refreshAuthenticatedUser(queryClient);
    },
  });
}

export function useRefreshAuthenticatedUserMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    // 重试必须执行到查询边界并返回 paused/failed，不能被 mutation 自身离线暂停。
    networkMode: "always",
    mutationFn: () => refreshAuthenticatedUser(queryClient),
  });
}

export function useChangePasswordMutation() {
  return useMutation({ mutationFn: changePassword });
}

export function useRegisterMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: RegisterInput) => register(input),
    onSuccess: (user) => {
      setAuthenticatedIdentity(queryClient, user);
    },
  });
}

export function useLogoutMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: logout,
    onSuccess: () => {
      clearAuthenticatedIdentity(queryClient);
    },
  });
}
