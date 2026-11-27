import { Loader2 } from "lucide-react";
import { lazy, Suspense } from "react";
import { Navigate, Outlet, Route, Routes } from "react-router-dom";

import { useAuth } from "../features/auth/AuthProvider";
import { LoginPage } from "../features/auth/LoginPage";
import {
  AccessDeniedPage,
  ProtectedRoute,
  PublicOnlyRoute,
  SkillAdminRoute,
  SuperAdminRoute,
} from "../features/auth/guards";
import { canManageSkills, isSuperAdmin } from "../features/auth/model";
import { DraftChatPage } from "../features/chat/DraftChatPage";
import { SessionChatPage } from "../features/chat/SessionChatPage";
import { AppShell } from "./AppShell";

// 非首屏路由页面按路由级代码分割懒加载，减小首屏主包体积同时保留认证与聊天主链路即开即用。
const PreferencesPage = lazy(() =>
  import("../features/preferences/PreferencesPage").then((m) => ({
    default: m.PreferencesPage,
  })),
);
const ProfilePage = lazy(() =>
  import("../features/auth/ProfilePage").then((m) => ({
    default: m.ProfilePage,
  })),
);
const ProjectsPage = lazy(() =>
  import("../features/projects/ProjectsPage").then((m) => ({
    default: m.ProjectsPage,
  })),
);
const ProjectWorkspacePage = lazy(() =>
  import("../features/projects/ProjectWorkspacePage").then((m) => ({
    default: m.ProjectWorkspacePage,
  })),
);
const KnowledgePage = lazy(() =>
  import("../features/knowledge/KnowledgePage").then((m) => ({
    default: m.KnowledgePage,
  })),
);
const KnowledgeDetailPage = lazy(() =>
  import("../features/knowledge/KnowledgeDetailPage").then((m) => ({
    default: m.KnowledgeDetailPage,
  })),
);
const PersonalSkillsPage = lazy(() =>
  import("../features/skills/PersonalSkillsPage").then((m) => ({
    default: m.PersonalSkillsPage,
  })),
);
const AdminModelsPage = lazy(() =>
  import("../features/admin/AdminModelsPage").then((m) => ({
    default: m.AdminModelsPage,
  })),
);
const AdminMcpPage = lazy(() =>
  import("../features/admin/AdminMcpPage").then((m) => ({
    default: m.AdminMcpPage,
  })),
);
const AdminSystemPromptsPage = lazy(() =>
  import("../features/admin/AdminSystemPromptsPage").then((m) => ({
    default: m.AdminSystemPromptsPage,
  })),
);
const AdminToolsPage = lazy(() =>
  import("../features/admin/AdminToolsPage").then((m) => ({
    default: m.AdminToolsPage,
  })),
);
const AdminSkillsPage = lazy(() =>
  import("../features/skills/AdminSkillsPage").then((m) => ({
    default: m.AdminSkillsPage,
  })),
);
const AdminSubagentsPage = lazy(() =>
  import("../features/admin/AdminSubagentsPage").then((m) => ({
    default: m.AdminSubagentsPage,
  })),
);

function RouteLoadingFallback() {
  return (
    <div className="mx-auto flex min-h-[260px] w-full max-w-4xl items-center justify-center px-6 py-12">
      <div
        className="inline-flex items-center gap-2.5 rounded-xl border border-border-subtle bg-canvas px-4 py-2.5 text-sm text-text-secondary shadow-xs"
        role="status"
      >
        <Loader2
          aria-hidden="true"
          className="h-4 w-4 animate-spin text-brand motion-reduce:animate-none"
        />
        <span>正在加载页面…</span>
      </div>
    </div>
  );
}

// 在 AppShell 内层承载懒加载路由边界，切页时保持侧栏与主框架常驻不闪烁。
function LazyRouteBoundary() {
  return (
    <Suspense fallback={<RouteLoadingFallback />}>
      <Outlet />
    </Suspense>
  );
}

/** 管理后台索引定向：超级管理员进入模型配置，部门管理员进入技能管理，普通员工进入无权限提示。 */
function AdminIndexRedirect() {
  const { skillAdminAccessDenied, superAdminAccessDenied, user } = useAuth();
  if (isSuperAdmin(user) && !superAdminAccessDenied) {
    return <Navigate replace to="/admin/models" />;
  }
  if (canManageSkills(user) && !skillAdminAccessDenied) {
    return <Navigate replace to="/admin/skills" />;
  }
  return <AccessDeniedPage />;
}

export function AppRoutes() {
  return (
    <Suspense fallback={<RouteLoadingFallback />}>
      <Routes>
        <Route element={<Navigate replace to="/chat" />} path="/" />

        <Route element={<PublicOnlyRoute />}>
          <Route element={<LoginPage />} path="/login" />
        </Route>

        <Route element={<ProtectedRoute />}>
          <Route element={<AppShell />}>
            <Route element={<DraftChatPage />} path="/chat" />
            <Route element={<DraftChatPage />} path="/chat/new" />
            <Route element={<SessionChatPage />} path="/chat/:sessionId" />

            <Route element={<LazyRouteBoundary />}>
              <Route element={<PreferencesPage />} path="/preferences" />
              <Route element={<ProfilePage />} path="/profile" />
              <Route element={<ProjectsPage />} path="/projects" />
              <Route
                element={<ProjectWorkspacePage />}
                path="/projects/:projectId"
              />
              <Route element={<KnowledgePage />} path="/knowledge" />
              <Route element={<PersonalSkillsPage />} path="/skills" />
              <Route
                element={<KnowledgeDetailPage />}
                path="/knowledge/:baseId"
              />

              <Route path="/admin">
                <Route element={<AdminIndexRedirect />} index />
                <Route element={<SkillAdminRoute />}>
                  <Route element={<AdminSkillsPage />} path="skills" />
                </Route>
                <Route element={<SuperAdminRoute />}>
                  <Route element={<AdminModelsPage />} path="models" />
                  <Route element={<AdminMcpPage />} path="mcp" />
                  <Route
                    element={<AdminSystemPromptsPage />}
                    path="system-prompts"
                  />
                  <Route element={<AdminToolsPage />} path="tools" />
                  <Route element={<AdminSubagentsPage />} path="subagents" />
                </Route>
              </Route>
            </Route>
          </Route>
        </Route>

        <Route element={<Navigate replace to="/chat" />} path="*" />
      </Routes>
    </Suspense>
  );
}
