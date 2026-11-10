import { useState } from "react";
import { NavLink, useLocation } from "react-router-dom";

import { Icon } from "../../shared/ui/Icons";
import { NewKnowledgeBaseDialog } from "./KnowledgeBaseDialogs";
import { useKnowledgeDirectory } from "./hooks";

function activeKnowledgeBaseId(pathname: string): string | null {
  const match = /^\/knowledge\/([^/]+)/.exec(pathname);
  if (!match) {
    return null;
  }
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

export function KnowledgeSidebar() {
  const location = useLocation();
  const bases = useKnowledgeDirectory();
  const [newBaseOpen, setNewBaseOpen] = useState(false);
  const activeBaseId = activeKnowledgeBaseId(location.pathname);

  return (
    <>
      <button
        className="sidebar-new-project"
        onClick={() => setNewBaseOpen(true)}
        type="button"
      >
        <Icon name="plus" size={15} />
        <span>新建知识库</span>
      </button>

      <div className="sidebar-scroll">
        <section className="sidebar-section" aria-labelledby="knowledge-nav-title">
          <div className="sidebar-section-title-row">
            <span id="knowledge-nav-title">知识库</span>
          </div>
          {bases.isPending ? (
            <p className="tree-state">正在加载知识库…</p>
          ) : bases.isError ? (
            <p className="tree-state tree-state--error" role="alert">
              知识库加载失败
            </p>
          ) : bases.data.length === 0 ? (
            <p className="tree-state">还没有知识库</p>
          ) : (
            <nav className="knowledge-sidebar-list" aria-label="知识库列表">
              {bases.data.map((base) => (
                <NavLink
                  className={({ isActive }) =>
                    `knowledge-sidebar-link${isActive ? " is-active" : ""}`
                  }
                  key={base.id}
                  to={`/knowledge/${encodeURIComponent(base.id)}`}
                >
                  <span>{base.name}</span>
                  {activeBaseId === base.id ? (
                    <span
                      aria-hidden="true"
                      className="project-center-active-dot"
                    />
                  ) : null}
                </NavLink>
              ))}
            </nav>
          )}
        </section>
      </div>

      <NewKnowledgeBaseDialog
        onClose={() => setNewBaseOpen(false)}
        open={newBaseOpen}
      />
    </>
  );
}
