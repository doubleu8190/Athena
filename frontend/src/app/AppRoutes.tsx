import { lazy, Suspense } from "react";
import { Navigate, Outlet, Route, Routes } from "react-router-dom";

import { LoginPage } from "../features/auth/LoginPage";
import { RegisterPage } from "../features/auth/RegisterPage";
import {
  AdminRoute,
  ProtectedRoute,
  PublicOnlyRoute,
} from "../features/auth/guards";
import { DraftChatPage } from "../features/chat/DraftChatPage";
import { SessionChatPage } from "../features/chat/SessionChatPage";
import { AppShell } from "./AppShell";

// 非首屏路由页面按路由级代码分割懒加载，减小首屏主包体积同时保留认证与聊天主链路即开即用。
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
const ProjectIndexRedirect = lazy(() =>
  import("../features/projects/ProjectWorkspacePage").then((m) => ({
    default: m.ProjectIndexRedirect,
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
    <div className="page-shell">
      <div className="page-loading" role="status">
        正在加载页面…
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

export function AppRoutes() {
  return (
    <Suspense fallback={<RouteLoadingFallback />}>
      <Routes>
        <Route element={<Navigate replace to="/chat" />} path="/" />

        <Route element={<PublicOnlyRoute />}>
          <Route element={<LoginPage />} path="/login" />
          <Route element={<RegisterPage />} path="/register" />
        </Route>

        <Route element={<ProtectedRoute />}>
          <Route element={<AppShell />}>
            <Route element={<DraftChatPage />} path="/chat" />
            <Route element={<DraftChatPage />} path="/chat/new" />
            <Route element={<SessionChatPage />} path="/chat/:sessionId" />

            <Route element={<LazyRouteBoundary />}>
              <Route element={<ProfilePage />} path="/profile" />
              <Route element={<ProjectsPage />} path="/projects" />
              <Route
                element={<ProjectIndexRedirect />}
                path="/projects/:projectId"
              />
              <Route
                element={<ProjectWorkspacePage />}
                path="/projects/:projectId/:tab"
              />
              <Route element={<KnowledgePage />} path="/knowledge" />
              <Route element={<PersonalSkillsPage />} path="/skills" />
              <Route
                element={<KnowledgeDetailPage />}
                path="/knowledge/:baseId"
              />

              <Route element={<AdminRoute />} path="/admin">
                <Route
                  element={<Navigate replace to="/admin/models" />}
                  index
                />
                <Route element={<AdminModelsPage />} path="models" />
                <Route element={<AdminMcpPage />} path="mcp" />
                <Route
                  element={<AdminSystemPromptsPage />}
                  path="system-prompts"
                />
                <Route element={<AdminToolsPage />} path="tools" />
                <Route element={<AdminSkillsPage />} path="skills" />
                <Route element={<AdminSubagentsPage />} path="subagents" />
              </Route>
            </Route>
          </Route>
        </Route>

        <Route element={<Navigate replace to="/chat" />} path="*" />
      </Routes>
    </Suspense>
  );
}
