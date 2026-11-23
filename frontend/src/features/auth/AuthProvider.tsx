import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
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
  type AdminForbiddenEventDetail,
} from "../../shared/api/client";
import { ADMIN_QUERY_ROOT } from "../../app/queryClient";
import { getCurrentUser } from "./api";
import { clearAuthenticatedIdentity } from "./cacheBoundary";
import { authKeys, isDeptAdmin, type User } from "./model";

export type AuthStatus =
  | "anonymous"
  | "authenticated"
  | "error"
  | "loading";

interface AuthContextValue {
  /** 兼容别名，语义等同于 superAdminAccessDenied（供超管页面导航阻塞等复用）。 */
  adminAccessDenied: boolean;
  clearSessionExpired: () => void;
  error: unknown;
  refetch: () => void;
  resetAccessDenied: () => void;
  sessionExpired: boolean;
  skillAdminAccessDenied: boolean;
  status: AuthStatus;
  superAdminAccessDenied: boolean;
  user: User | null;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

type AdminForbiddenScope =
  | "global"
  | "super_admin_only"
  | "skill_collection"
  | "skill_resource";

function normalizeAdminPath(rawPath: string): string {
  const withoutQuery = rawPath.split(/[?#]/, 1)[0] ?? "";
  const withoutPrefix = withoutQuery.startsWith("/api/v1/")
    ? withoutQuery.slice("/api/v1".length)
    : withoutQuery;
  if (withoutPrefix.length > 1 && withoutPrefix.endsWith("/")) {
    return withoutPrefix.slice(0, -1);
  }
  return withoutPrefix;
}

/**
 * 将触发 403 admin_required 的接口路径分级：
 * - global：未附带路径的兼容事件，按全局管理拒止处理；
 * - super_admin_only：仅限超管访问的接口（含技能执行日志排查 /admin/skills/executions*）；
 * - skill_collection：技能管理集合入口 /admin/skills；
 * - skill_resource：单个技能资源或操作端点 /admin/skills/{id}...。
 */
function classifyAdminForbiddenPath(
  rawPath: string | undefined,
): AdminForbiddenScope {
  if (!rawPath) {
    return "global";
  }
  const pathname = normalizeAdminPath(rawPath);
  if (
    pathname === "/admin/skills/executions" ||
    pathname.startsWith("/admin/skills/executions/")
  ) {
    return "super_admin_only";
  }
  if (pathname === "/admin/skills") {
    return "skill_collection";
  }
  if (pathname.startsWith("/admin/skills/")) {
    return "skill_resource";
  }
  return "super_admin_only";
}

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
  const [superAdminAccessDenied, setSuperAdminAccessDenied] = useState(false);
  const [skillAdminAccessDenied, setSkillAdminAccessDenied] = useState(false);
  const [sessionExpired, setSessionExpired] = useState(false);

  const clearSessionExpired = useCallback(() => {
    setSessionExpired(false);
  }, []);

  const resetAccessDenied = useCallback(() => {
    setSuperAdminAccessDenied(false);
    setSkillAdminAccessDenied(false);
  }, []);

  const query = useQuery<User | null>({
    queryKey: authKeys.currentUser,
    queryFn: loadCurrentUser,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 5 * 60 * 1000,
  });

  useEffect(() => {
    const handleUnauthorized = () => {
      // 仅当缓存中存在已登录用户时，401 才表示会话被吊销或过期（例如离职停用/会话失效）；
      // 匿名用户在登录页输错密码触发 401 invalid_credentials 不应误置过期提示。
      const wasAuthenticated = Boolean(
        queryClient.getQueryData<User | null>(authKeys.currentUser),
      );
      setSuperAdminAccessDenied(false);
      setSkillAdminAccessDenied(false);
      if (wasAuthenticated) {
        setSessionExpired(true);
      }
      // 401 属于身份边界：清除所有业务缓存并置为匿名，不重新发请求避免循环。
      clearAuthenticatedIdentity(queryClient);
    };
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, handleUnauthorized);
    return () => {
      window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, handleUnauthorized);
    };
  }, [queryClient]);

  useEffect(() => {
    const handleAdminForbidden = (event: Event) => {
      const detail =
        event instanceof CustomEvent
          ? (event.detail as
              | Partial<AdminForbiddenEventDetail>
              | null
              | undefined)
          : undefined;
      const scope = classifyAdminForbiddenPath(
        typeof detail?.path === "string" ? detail.path : undefined,
      );
      const cachedUser = queryClient.getQueryData<User | null>(
        authKeys.currentUser,
      );
      const cachedIsDeptAdmin = isDeptAdmin(cachedUser);

      if (scope === "super_admin_only" && cachedIsDeptAdmin) {
        // 部门管理员触达超管专属端点（含技能执行排查）时，仅阻断超管路由并清理超管缓存，
        // 保留 ["admin", "skills"] 缓存且不置位 skillAdminAccessDenied，避免 /admin/skills 闪断或锁死。
        setSuperAdminAccessDenied(true);
        queryClient.removeQueries({
          predicate: (cachedQuery) =>
            cachedQuery.queryKey[0] === ADMIN_QUERY_ROOT[0] &&
            cachedQuery.queryKey[1] !== "skills",
        });
        return;
      }

      if (scope === "skill_resource" && cachedIsDeptAdmin) {
        // 部门管理员对单个非管辖/公司级技能收到 403 时，仅静默复核 /auth/me，
        // 不卸载整个技能管理页；若确已降权为 member，/auth/me 返回后由 SkillAdminRoute 自动拦截。
        void queryClient.invalidateQueries({
          exact: true,
          queryKey: authKeys.currentUser,
        });
        return;
      }

      // 集合级 /admin/skills、全局无路径事件或超管被拒时立即 fail-closed 阻断并清缓存复核身份。
      setSuperAdminAccessDenied(true);
      setSkillAdminAccessDenied(true);
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
    // 当前用户查询成功返回后代表身份已完成核验，是否仍有管理权限由最新 role 决定，
    // 同时清除过往会话过期状态与权限拒止标记。
    if (query.isSuccess && query.data) {
      setSuperAdminAccessDenied(false);
      setSkillAdminAccessDenied(false);
      setSessionExpired(false);
    }
  }, [query.data, query.dataUpdatedAt, query.isSuccess]);

  const { data, error, isError, isPending, refetch } = query;
  const value = useMemo<AuthContextValue>(() => {
    let status: AuthStatus = "loading";
    if (data !== undefined) {
      // 后台复核失败时仍可使用已核验的身份快照；只有首轮无身份且请求失败才阻断工作区。
      status = data ? "authenticated" : "anonymous";
    } else if (isError) {
      status = "error";
    } else if (!isPending) {
      status = "anonymous";
    }

    return {
      adminAccessDenied: superAdminAccessDenied,
      clearSessionExpired,
      error,
      refetch: () => {
        void refetch();
      },
      resetAccessDenied,
      sessionExpired,
      skillAdminAccessDenied,
      status,
      superAdminAccessDenied,
      user: data ?? null,
    };
  }, [
    clearSessionExpired,
    data,
    error,
    isError,
    isPending,
    refetch,
    resetAccessDenied,
    sessionExpired,
    skillAdminAccessDenied,
    superAdminAccessDenied,
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

/** 供认证 Mutation 在不强耦合 Provider 的前提下同步重置 sessionExpired 与权限拒止状态。 */
export function useOptionalAuth(): AuthContextValue | undefined {
  return useContext(AuthContext);
}
