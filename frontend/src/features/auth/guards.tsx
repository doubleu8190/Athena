import { Navigate, Outlet, useLocation } from "react-router-dom";

import { AccessDeniedPage } from "../../pages/PlaceholderPages";
import { Brand } from "../../shared/ui/Brand";
import { Icon } from "../../shared/ui/Icons";
import { useAuth } from "./AuthProvider";
import { getSafeReturnTo } from "./returnTo";

function AuthLoadingScreen() {
  return (
    <main className="auth-status-page" aria-busy="true">
      <Brand />
      <div className="auth-status-card" role="status" aria-live="polite">
        <span className="auth-status-spinner" aria-hidden="true" />
        <div>
          <strong>正在确认登录状态</strong>
          <p>正在通过服务端 Cookie 恢复你的工作空间。</p>
        </div>
      </div>
    </main>
  );
}

function AuthErrorScreen() {
  const { refetch } = useAuth();
  return (
    <main className="auth-status-page">
      <Brand />
      <div className="auth-status-card auth-status-card--error">
        <span className="auth-status-icon">
          <Icon name="shield" size={22} />
        </span>
        <div>
          <strong>暂时无法确认登录状态</strong>
          <p>请确认后端服务已启动，然后重试。</p>
          <button className="button-secondary" onClick={refetch} type="button">
            重新连接
          </button>
        </div>
      </div>
    </main>
  );
}

export function ProtectedRoute() {
  const { status } = useAuth();
  const location = useLocation();

  if (status === "loading") {
    return <AuthLoadingScreen />;
  }
  if (status === "error") {
    return <AuthErrorScreen />;
  }
  if (status === "anonymous") {
    // 保留完整的站内路径，登录成功后可以回到原目标。
    const returnTo = `${location.pathname}${location.search}${location.hash}`;
    return <Navigate replace state={{ returnTo }} to="/login" />;
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
    return <Navigate replace to={getSafeReturnTo(location.state)} />;
  }
  return <Outlet />;
}

export function AdminRoute() {
  const { adminAccessDenied, user } = useAuth();
  if (adminAccessDenied || user?.role !== "admin") {
    return <AccessDeniedPage />;
  }
  return <Outlet />;
}
