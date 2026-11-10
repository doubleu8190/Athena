import { useState } from "react";
import { NavLink, useLocation } from "react-router-dom";

import { Icon } from "../../shared/ui/Icons";
import { NewProjectDialog } from "./ProjectNameDialogs";
import { useProjectDirectory } from "./hooks";

function activeProjectIdFromPath(pathname: string): string | null {
  const match = /^\/projects\/([^/]+)/.exec(pathname);
  if (!match) {
    return null;
  }
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

export function ProjectCenterSidebar() {
  const location = useLocation();
  const projects = useProjectDirectory();
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const activeProjectId = activeProjectIdFromPath(location.pathname);

  return (
    <>
      <button
        className="sidebar-new-project"
        onClick={() => setNewProjectOpen(true)}
        type="button"
      >
        <Icon name="plus" size={15} />
        <span>新建项目</span>
      </button>

      <div className="sidebar-scroll">
        <section
          className="sidebar-section"
          aria-labelledby="project-center-title"
        >
          <div className="sidebar-section-title-row">
            <span id="project-center-title">项目中心</span>
          </div>
          {projects.isPending ? (
            <p className="tree-state">正在加载项目…</p>
          ) : projects.isError ? (
            <p className="tree-state tree-state--error" role="alert">
              项目加载失败
            </p>
          ) : projects.data.length === 0 ? (
            <p className="tree-state">还没有项目</p>
          ) : (
            <nav className="project-center-list" aria-label="项目列表">
              {projects.data.map((project) => (
                <NavLink
                  className={({ isActive }) =>
                    `project-center-link${isActive ? " is-active" : ""}`
                  }
                  key={project.id}
                  to={`/projects/${encodeURIComponent(project.id)}/overview`}
                >
                  <span>{project.name}</span>
                  {activeProjectId === project.id ? (
                    <span className="project-center-active-dot" aria-hidden="true" />
                  ) : null}
                </NavLink>
              ))}
            </nav>
          )}
        </section>
      </div>

      <NewProjectDialog
        onClose={() => setNewProjectOpen(false)}
        open={newProjectOpen}
      />
    </>
  );
}

