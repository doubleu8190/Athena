import type { QueryClient } from "@tanstack/react-query";

import { advanceAuthIdentityEpoch } from "../../shared/api/client";
import { authKeys, type User } from "./model";

const AUTH_QUERY_ROOT = authKeys.currentUser[0];

function isProtectedQuery(query: {
  queryKey: readonly unknown[];
}): boolean {
  return query.queryKey[0] !== AUTH_QUERY_ROOT;
}

function clearProtectedQueries(queryClient: QueryClient): void {
  // Query.cancel 会同步拒绝进行中的 retryer；remove 再销毁查询和观察者，
  // 保证旧身份迟到响应无法回写，同时保留 auth 查询供当前 Provider 继续观察。
  void queryClient.cancelQueries(
    { predicate: isProtectedQuery },
    { silent: true },
  );
  queryClient.removeQueries({ predicate: isProtectedQuery });
}

function cancelCurrentUserQuery(queryClient: QueryClient): void {
  // 旧身份的 /auth/me 不能被移除，否则会破坏 Provider 的观察关系；
  // 但必须取消，避免其迟到结果覆盖新用户或把已确认的新身份改成错误态。
  void queryClient.cancelQueries(
    { exact: true, queryKey: authKeys.currentUser },
    { silent: true },
  );
}

export function setAuthenticatedIdentity(
  queryClient: QueryClient,
  user: User,
): void {
  // 先推进代次并清理旧业务缓存，再写入新身份；不清理 auth 根避免误伤 B。
  advanceAuthIdentityEpoch();
  cancelCurrentUserQuery(queryClient);
  clearProtectedQueries(queryClient);
  queryClient.setQueryData<User>(authKeys.currentUser, user);
}

export function clearAuthenticatedIdentity(
  queryClient: QueryClient,
): void {
  advanceAuthIdentityEpoch();
  cancelCurrentUserQuery(queryClient);
  clearProtectedQueries(queryClient);
  queryClient.setQueryData<User | null>(authKeys.currentUser, null);
}
