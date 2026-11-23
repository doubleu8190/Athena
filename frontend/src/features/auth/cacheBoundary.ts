import { CancelledError, type QueryClient } from "@tanstack/react-query";

import {
  ApiError,
  advanceAuthIdentityEpoch,
  authIdentityChangedError,
  getAuthIdentityEpoch,
  isAuthIdentityCurrent,
} from "../../shared/api/client";
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

/**
 * 统一完成登录身份切换（本地登录与 Mock 快捷登录共用），
 * 推进请求代次并清空全部受保护业务缓存，严格防止跨用户缓存串号。
 */
export function completeAuthenticatedIdentity(
  queryClient: QueryClient,
  user: User,
): void {
  setAuthenticatedIdentity(queryClient, user);
}

export function clearAuthenticatedIdentity(
  queryClient: QueryClient,
): void {
  advanceAuthIdentityEpoch();
  cancelCurrentUserQuery(queryClient);
  clearProtectedQueries(queryClient);
  queryClient.setQueryData<User | null>(authKeys.currentUser, null);
}

export type AuthenticatedUserRefreshResult =
  | { status: "refreshed" }
  | {
      error: unknown;
      reason:
        | "cancelled"
        | "paused"
        | "request_failed"
        | "superseded"
        | "unavailable";
      status: "failed";
    };

let authenticatedUserRefreshGeneration = 0;

function incompleteRefreshError(reason: string): Error {
  const error = new Error(`authenticated_user_refresh_${reason}`);
  error.name = "AuthenticatedUserRefreshIncompleteError";
  return error;
}

export async function refreshAuthenticatedUser(
  queryClient: QueryClient,
): Promise<AuthenticatedUserRefreshResult> {
  const identityEpoch = getAuthIdentityEpoch();
  const refreshGeneration = ++authenticatedUserRefreshGeneration;
  const previousDataUpdateCount =
    queryClient.getQueryState(authKeys.currentUser)?.dataUpdateCount ?? -1;
  try {
    await queryClient.refetchQueries(
      { exact: true, queryKey: authKeys.currentUser },
      {
        // PATCH 已提交后取消旧 /auth/me 并发起新核验，避免旧资料覆盖当前状态。
        cancelRefetch: true,
        // 必须让刷新失败进入调用方，才能区分写入成功与身份刷新失败。
        throwOnError: true,
      },
    );

    // 成功后仍需复核身份与刷新归属，避免旧请求搭上新请求或账号切换后的结果。
    if (!isAuthIdentityCurrent(identityEpoch)) {
      throw authIdentityChangedError();
    }
    if (refreshGeneration !== authenticatedUserRefreshGeneration) {
      return {
        error: incompleteRefreshError("superseded"),
        reason: "superseded",
        status: "failed",
      };
    }

    const queryState = queryClient.getQueryState(authKeys.currentUser);
    if (queryState?.fetchStatus === "paused") {
      return {
        error: incompleteRefreshError("paused"),
        reason: "paused",
        status: "failed",
      };
    }
    if (
      !queryState ||
      queryState.status !== "success" ||
      queryState.data === null ||
      queryState.data === undefined ||
      queryState.dataUpdateCount <= previousDataUpdateCount
    ) {
      return {
        error: incompleteRefreshError("unavailable"),
        reason: "unavailable",
        status: "failed",
      };
    }
    return { status: "refreshed" };
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      throw error;
    }
    // 账号切换或退出时，旧身份的普通刷新结果不得生成当前身份的重试提示。
    if (!isAuthIdentityCurrent(identityEpoch)) {
      throw authIdentityChangedError();
    }
    if (refreshGeneration !== authenticatedUserRefreshGeneration) {
      return {
        error,
        reason: "superseded",
        status: "failed",
      };
    }
    return {
      error,
      reason:
        error instanceof CancelledError ? "cancelled" : "request_failed",
      status: "failed",
    };
  }
}
