import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type PropsWithChildren,
} from "react";

import {
  ADMIN_FORBIDDEN_EVENT,
  AUTH_UNAUTHORIZED_EVENT,
  ApiError,
} from "../../shared/api/client";
import { ADMIN_QUERY_ROOT } from "../../app/queryClient";
import { getCurrentUser } from "./api";
import { clearAuthenticatedIdentity } from "./cacheBoundary";
import { authKeys, type User } from "./model";

export type AuthStatus =
  | "anonymous"
  | "authenticated"
  | "error"
  | "loading";

interface AuthContextValue {
  adminAccessDenied: boolean;
  error: unknown;
  refetch: () => void;
  status: AuthStatus;
  user: User | null;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

async function loadCurrentUser(): Promise<User | null> {
  try {
    return await getCurrentUser();
  } catch (error) {
    // Cookie 不存在或已失效是正常的匿名状态，不进入全局错误页。
    if (error instanceof ApiError && error.status === 401) {
      return null;
    }
    throw error;
  }
}

export function AuthProvider({ children }: PropsWithChildren) {
  const queryClient = useQueryClient();
  const [adminAccessDenied, setAdminAccessDenied] = useState(false);
  const query = useQuery<User | null>({
    queryKey: authKeys.currentUser,
    queryFn: loadCurrentUser,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 5 * 60 * 1000,
  });

  useEffect(() => {
    const handleUnauthorized = () => {
      // 401 属于身份边界：清除所有业务缓存并置为匿名，不重新发请求避免循环。
      setAdminAccessDenied(false);
      clearAuthenticatedIdentity(queryClient);
    };
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, handleUnauthorized);
    return () => {
      window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, handleUnauthorized);
    };
  }, [queryClient]);

  useEffect(() => {
    const handleAdminForbidden = () => {
      // 后端已明确拒绝当前管理员身份，先移除所有管理数据，再重新核验当前用户。
      setAdminAccessDenied(true);
      queryClient.removeQueries({ queryKey: ADMIN_QUERY_ROOT });
      void queryClient.invalidateQueries({
        exact: true,
        queryKey: authKeys.currentUser,
      });
    };
    window.addEventListener(ADMIN_FORBIDDEN_EVENT, handleAdminForbidden);
    return () => {
      window.removeEventListener(
        ADMIN_FORBIDDEN_EVENT,
        handleAdminForbidden,
      );
    };
  }, [queryClient]);

  useEffect(() => {
    // 当前用户查询成功返回后代表身份已完成核验，是否仍有管理权限由最新 role 决定。
    if (query.isSuccess && query.data) {
      setAdminAccessDenied(false);
    }
  }, [query.data, query.dataUpdatedAt, query.isSuccess]);

  const value = useMemo<AuthContextValue>(() => {
    let status: AuthStatus = "loading";
    if (query.isError) {
      status = "error";
    } else if (!query.isPending) {
      status = query.data ? "authenticated" : "anonymous";
    }

    return {
      adminAccessDenied,
      error: query.error,
      refetch: () => {
        void query.refetch();
      },
      status,
      user: query.data ?? null,
    };
  }, [
    adminAccessDenied,
    query.data,
    query.error,
    query.isError,
    query.isPending,
    query.refetch,
  ]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return context;
}
