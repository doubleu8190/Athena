import { Navigate, Route, Routes } from "react-router-dom";

import { LoginPage } from "../features/auth/LoginPage";
import { RegisterPage } from "../features/auth/RegisterPage";
import {
  AdminRoute,
  ProtectedRoute,
  PublicOnlyRoute,
} from "../features/auth/guards";
import { DraftChatPage } from "../features/chat/DraftChatPage";
import { SessionChatPage } from "../features/chat/SessionChatPage";
import { AdminMcpPage } from "../features/admin/AdminMcpPage";
import { AdminModelsPage } from "../features/admin/AdminModelsPage";
import { AdminToolsPage } from "../features/admin/AdminToolsPage";
import { KnowledgeDetailPage } from "../features/knowledge/KnowledgeDetailPage";
import { KnowledgePage } from "../features/knowledge/KnowledgePage";
import { ProjectsPage } from "../features/projects/ProjectsPage";
import {
  ProjectIndexRedirect,
  ProjectWorkspacePage,
} from "../features/projects/ProjectWorkspacePage";
import { AppShell } from "./AppShell";

export function AppRoutes() {
  return (
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
          <Route
            element={<KnowledgeDetailPage />}
            path="/knowledge/:baseId"
          />

          <Route element={<AdminRoute />} path="/admin">
            <Route element={<Navigate replace to="/admin/models" />} index />
            <Route element={<AdminModelsPage />} path="models" />
            <Route element={<AdminMcpPage />} path="mcp" />
            <Route element={<AdminToolsPage />} path="tools" />
          </Route>
        </Route>
      </Route>

      <Route element={<Navigate replace to="/chat" />} path="*" />
    </Routes>
  );
}
