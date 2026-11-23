import { useMutation, useQueryClient } from "@tanstack/react-query";

import {
  changePassword,
  login,
  logout,
  mockLogin,
  updateCurrentUser,
} from "./api";
import { useOptionalAuth } from "./AuthProvider";
import {
  clearAuthenticatedIdentity,
  completeAuthenticatedIdentity,
  refreshAuthenticatedUser,
} from "./cacheBoundary";
import type { LoginInput } from "./model";

export function useLoginMutation() {
  const queryClient = useQueryClient();
  const auth = useOptionalAuth();
  return useMutation({
    mutationFn: (input: LoginInput) => login(input),
    onSuccess: (user) => {
      // 登录成功后清理历史业务缓存，并同步复位会话过期与权限拒止状态，避免跨账号首帧瞬闪。
      completeAuthenticatedIdentity(queryClient, user);
      auth?.resetAccessDenied();
      auth?.clearSessionExpired();
    },
  });
}

export function useMockLoginMutation() {
  const queryClient = useQueryClient();
  const auth = useOptionalAuth();
  return useMutation({
    mutationFn: (userId: string) => mockLogin(userId),
    onSuccess: (user) => {
      // 与本地登录复用同一身份切换与缓存清理边界，严格防止非生产切换角色时串号与权限残留。
      completeAuthenticatedIdentity(queryClient, user);
      auth?.resetAccessDenied();
      auth?.clearSessionExpired();
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

export function useLogoutMutation() {
  const queryClient = useQueryClient();
  const auth = useOptionalAuth();
  return useMutation({
    mutationFn: logout,
    onSuccess: () => {
      // 主动退出清空全部受保护缓存并复位权限拒止与过期标记，不误触发“会话已过期”横幅。
      auth?.resetAccessDenied();
      auth?.clearSessionExpired();
      clearAuthenticatedIdentity(queryClient);
    },
  });
}
