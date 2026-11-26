import { Loader2 } from "lucide-react";
import { Navigate, Outlet, useLocation } from "react-router-dom";

import { AccessDeniedPage } from "../../pages/PlaceholderPages";
import { Brand, Button, InlineAlert } from "../../shared/ui";
import { useAuth } from "./AuthProvider";
import { canManageSkills, isSuperAdmin } from "./model";
import { getSafeReturnTo } from "./returnTo";

function AuthLoadingScreen() {
  return (
    <main
      aria-busy="true"
      className="flex min-h-dvh flex-col items-center justify-center bg-subtle px-4 py-10"
    >
      <div className="flex w-full max-w-[380px] flex-col items-center gap-6">
        <Brand />
        <div
          aria-live="polite"
          className="flex w-full items-center gap-3.5 rounded-2xl border border-border-subtle bg-canvas p-5 shadow-sm"
          role="status"
        >
          <Loader2
            aria-hidden="true"
            className="h-5 w-5 shrink-0 animate-spin text-brand motion-reduce:animate-none"
          />
          <div className="min-w-0">
            <p className="text-sm font-medium text-text-primary">
              正在确认登录状态
            </p>
            <p className="mt-0.5 text-xs text-text-secondary">
              正在恢复你的企业工作空间，请稍候。
            </p>
          </div>
        </div>
      </div>
    </main>
  );
}

function AuthErrorScreen() {
  const { refetch } = useAuth();
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center bg-subtle px-4 py-10">
      <div className="flex w-full max-w-[420px] flex-col items-center gap-6">
        <Brand />
        <div className="w-full rounded-2xl border border-border-subtle bg-canvas p-6 shadow-sm">
          <InlineAlert
            description="请确认后端服务已启动，然后重试。"
            title="暂时无法确认登录状态"
            variant="danger"
          />
          <div className="mt-4 flex justify-end">
            <Button onClick={refetch} size="sm" variant="secondary">
              重新连接
            </Button>
          </div>
        </div>
      </div>
    </main>
  );
}

export function ProtectedRoute() {
  const { sessionExpired, status } = useAuth();
  const location = useLocation();

  if (status === "loading") {
    return <AuthLoadingScreen />;
  }
  if (status === "error") {
    return <AuthErrorScreen />;
  }
  if (status === "anonymous") {
    // 保留完整的站内路径以便重新登录后返回；若是已登录态被 401 吊销，携带过期原因提示。
    const returnTo = `${location.pathname}${location.search}${location.hash}`;
    const loginPath = sessionExpired ? "/login?reason=expired" : "/login";
    return <Navigate replace state={{ returnTo }} to={loginPath} />;
  }
  return <Outlet />;
}

export function PublicOnlyRoute() {
  const { status } = useAuth();
  const location = useLocation();

  if (status === "loading") {
    return <AuthLoadingScreen />;
  }
  if (status === "error") {
    return <AuthErrorScreen />;
  }
  if (status === "authenticated") {
    return (
      <Navigate replace to={getSafeReturnTo(location.state, location.search)} />
    );
  }
  return <Outlet />;
}

/** 仅允许企业超级管理员访问的路由守卫（模型、MCP、提示词、工具审批、子智能体、组织通讯录）。 */
export function SuperAdminRoute() {
  const { superAdminAccessDenied, user } = useAuth();
  if (superAdminAccessDenied || !isSuperAdmin(user)) {
    return <AccessDeniedPage />;
  }
  return <Outlet />;
}

/** 允许超级管理员与部门管理员访问的技能管理守卫（/admin/skills）。 */
export function SkillAdminRoute() {
  const { skillAdminAccessDenied, user } = useAuth();
  if (skillAdminAccessDenied || !canManageSkills(user)) {
    return <AccessDeniedPage />;
  }
  return <Outlet />;
}
