import { useState } from "react";
import { Link } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { projectErrorMessage } from "./errorMessages";
import { useProjectDirectory } from "./hooks";
import { NewProjectDialog } from "./ProjectNameDialogs";

export function ProjectsPage() {
  const projects = useProjectDirectory();
  const [newProjectOpen, setNewProjectOpen] = useState(false);

  return (
    <div className="page-shell projects-page">
      <header className="page-heading">
        <div className="page-heading-copy">
          <p className="page-eyebrow">Workspace / Projects</p>
          <h1>项目</h1>
          <p className="page-description">
            项目把同一主题下的对话组织在一起。这里只展示当前用户拥有的真实项目。
          </p>
        </div>
        <button
          className="button-primary"
          onClick={() => setNewProjectOpen(true)}
          type="button"
        >
          <Icon name="plus" size={14} />
          新建项目
        </button>
      </header>

      {projects.isPending ? (
        <div className="page-loading" role="status">
          正在加载项目…
        </div>
      ) : projects.isError ? (
        <ErrorBanner
          message={projectErrorMessage(projects.error)}
          onRetry={() => void projects.refetch()}
        />
      ) : projects.data.length === 0 ? (
        <EmptyState
          action={
            <button
              className="button-primary"
              onClick={() => setNewProjectOpen(true)}
              type="button"
            >
              <Icon name="plus" size={14} />
              新建项目
            </button>
          }
          description="创建第一个项目，然后把新对话绑定到项目上下文。"
          icon="folder"
          kicker="No projects yet"
          title="还没有项目"
        />
      ) : (
        <section aria-labelledby="project-list-title">
          <div className="surface-header project-list-header">
            <div className="surface-title">
              <strong id="project-list-title">全部项目</strong>
              <span>按最近更新时间排列</span>
            </div>
          </div>
          <ul className="project-list-grid">
            {projects.data.map((project) => (
              <li key={project.id}>
                <Link
                  className="project-list-card"
                  to={`/projects/${encodeURIComponent(project.id)}/overview`}
                >
                  <span className="project-list-card-top">
                    <strong>{project.name}</strong>
                    <Icon name="arrow-right" size={16} />
                  </span>
                  <span className="project-list-time">
                    创建于 {formatDateTime(project.created_at)}
                  </span>
                  <span className="project-list-time">
                    更新于 {formatDateTime(project.updated_at)}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}

      <NewProjectDialog
        onClose={() => setNewProjectOpen(false)}
        open={newProjectOpen}
      />
    </div>
  );
}

