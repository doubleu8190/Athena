import { Navigate, NavLink, useParams } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { newChatHref } from "../chat/draft";
import {
  isProjectNotFound,
  projectErrorMessage,
} from "./errorMessages";
import { useProject } from "./hooks";
import type { Project } from "./model";
import { ProjectKnowledgeTab } from "./ProjectKnowledgeTab";
import { ProjectOverviewTab } from "./ProjectOverviewTab";
import { ProjectSessionsTab } from "./ProjectSessionsTab";
import { ProjectSettingsTab } from "./ProjectSettingsTab";

const PROJECT_TABS = [
  { key: "overview", label: "概览" },
  { key: "sessions", label: "会话" },
  { key: "knowledge", label: "知识库" },
  { key: "settings", label: "设置" },
] as const;

type ProjectTab = (typeof PROJECT_TABS)[number]["key"];

function isProjectTab(value: string | undefined): value is ProjectTab {
  return PROJECT_TABS.some((tab) => tab.key === value);
}

export function ProjectIndexRedirect() {
  const { projectId } = useParams<{ projectId: string }>();
  return (
    <Navigate
      replace
      to={`/projects/${encodeURIComponent(projectId ?? "")}/overview`}
    />
  );
}

interface ProjectWorkspaceProps {
  projectId: string;
  tab: ProjectTab;
}

function ProjectWorkspace({ projectId, tab }: ProjectWorkspaceProps) {
  const project = useProject(projectId);

  if (project.isPending) {
    return (
      <div className="page-shell">
        <div className="page-loading" role="status">
          正在加载项目…
        </div>
      </div>
    );
  }

  if (project.isError) {
    const notFound = isProjectNotFound(project.error);
    return (
      <div className="page-shell project-error-page">
        <EmptyState
          action={
            <NavLink className="button-primary" to="/projects">
              返回项目中心
            </NavLink>
          }
          description={
            notFound
              ? "该项目不存在、已删除，或不属于当前账号。"
              : "项目信息暂时无法加载，请稍后重试。"
          }
          icon="folder"
          kicker={notFound ? "404 / Project" : "Request failed"}
          title={notFound ? "项目不存在或不可访问" : "项目加载失败"}
        />
        {!notFound ? (
          <ErrorBanner
            message={projectErrorMessage(project.error)}
            onRetry={() => void project.refetch()}
          />
        ) : null}
      </div>
    );
  }

  return (
    <ProjectWorkspaceContent project={project.data} tab={tab} />
  );
}

interface ProjectWorkspaceContentProps {
  project: Project;
  tab: ProjectTab;
}

function ProjectWorkspaceContent({
  project,
  tab,
}: ProjectWorkspaceContentProps) {
  const basePath = `/projects/${encodeURIComponent(project.id)}`;
  return (
    <div className="page-shell project-workspace">
      <header className="project-hero">
        <div className="project-hero-top">
          <div className="project-hero-copy">
            <p className="page-eyebrow">Active project</p>
            <h1>{project.name}</h1>
            <p className="project-meta">
              创建于 {formatDateTime(project.created_at)} · 更新于{" "}
              {formatDateTime(project.updated_at)}
            </p>
          </div>
          <div className="project-actions">
            <NavLink className="button-quiet" to={`${basePath}/settings`}>
              <Icon name="settings" size={14} />
              项目设置
            </NavLink>
            <NavLink
              className="button-primary"
              to={newChatHref(project.id)}
            >
              开始对话
              <Icon name="arrow-right" size={14} />
            </NavLink>
          </div>
        </div>

        <nav className="project-tabs" aria-label="项目页面">
          {PROJECT_TABS.map((item) => (
            <NavLink
              className={({ isActive }) =>
                `project-tab${isActive ? " is-active" : ""}`
              }
              key={item.key}
              to={`${basePath}/${item.key}`}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </header>

      {tab === "overview" ? <ProjectOverviewTab project={project} /> : null}
      {tab === "sessions" ? <ProjectSessionsTab project={project} /> : null}
      {tab === "knowledge" ? (
        <ProjectKnowledgeTab project={project} />
      ) : null}
      {tab === "settings" ? <ProjectSettingsTab project={project} /> : null}
    </div>
  );
}

export function ProjectWorkspacePage() {
  const { projectId, tab } = useParams<{
    projectId: string;
    tab: string;
  }>();
  if (!projectId) {
    return <Navigate replace to="/projects" />;
  }
  if (!isProjectTab(tab)) {
    return (
      <Navigate
        replace
        to={`/projects/${encodeURIComponent(projectId)}/overview`}
      />
    );
  }
  return <ProjectWorkspace projectId={projectId} tab={tab} />;
}

