import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";

import { NewProjectDialog } from "../projects/ProjectNameDialogs";
import { useProjectDirectory } from "../projects/hooks";
import type { Project } from "../projects/model";
import { formatShortDateTime } from "../../shared/lib/format";
import { Icon } from "../../shared/ui/Icons";
import { useSession, useSessionList } from "../sessions/hooks";
import { sessionDisplayTitle, type Session } from "../sessions/model";
import { useDraftChat } from "./DraftChatContext";
import { newChatHref } from "./draft";

const RECENT_SECTION_STORAGE_KEY = "athena.chat.recent-collapsed";

function readRecentExpanded(): boolean {
  try {
    return window.localStorage.getItem(RECENT_SECTION_STORAGE_KEY) !== "collapsed";
  } catch {
    return true;
  }
}

function activeSessionIdFromPath(pathname: string): string | null {
  const match = /^\/chat\/([^/]+)$/.exec(pathname);
  if (!match || match[1] === "new") {
    return null;
  }
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

interface SessionLinkProps {
  activeSessionId: string | null;
  session: Session;
}

function SessionLink({ activeSessionId, session }: SessionLinkProps) {
  const isActive = activeSessionId === session.id;
  return (
    <Link
      aria-current={isActive ? "page" : undefined}
      className={`tree-session${isActive ? " is-active" : ""}`}
      title={sessionDisplayTitle(session.title)}
      to={`/chat/${encodeURIComponent(session.id)}`}
    >
      {sessionDisplayTitle(session.title)}
    </Link>
  );
}

interface ProjectTreeNodeProps {
  activeProjectId: string | null;
  activeSessionId: string | null;
  project: Project;
}

function ProjectTreeNode({
  activeProjectId,
  activeSessionId,
  project,
}: ProjectTreeNodeProps) {
  const [expanded, setExpanded] = useState(activeProjectId === project.id);
  const sessions = useSessionList(
    { page: 1, pageSize: 5, projectId: project.id },
    expanded,
  );

  useEffect(() => {
    if (activeProjectId === project.id) {
      setExpanded(true);
    }
  }, [activeProjectId, project.id]);

  const regionId = `project-tree-${project.id}`;

  return (
    <div className={`tree-node${expanded ? " is-open" : ""}`}>
      <button
        aria-controls={regionId}
        aria-expanded={expanded}
        aria-label={`项目 ${project.name}`}
        className="tree-project-toggle"
        onClick={() => setExpanded((value) => !value)}
        type="button"
      >
        <Icon name="folder" size={14} />
        <span>{project.name}</span>
        <Icon
          className="tree-chevron"
          name="chevron"
          size={12}
        />
      </button>
      {expanded ? (
        <div className="tree-sessions" id={regionId}>
          {sessions.isPending ? (
            <p className="tree-state">正在加载会话…</p>
          ) : sessions.isError ? (
            <p className="tree-state tree-state--error" role="alert">
              会话加载失败
            </p>
          ) : sessions.data.items.length === 0 ? (
            <p className="tree-state">暂无会话</p>
          ) : (
            sessions.data.items.map((session) => (
              <SessionLink
                activeSessionId={activeSessionId}
                key={session.id}
                session={session}
              />
            ))
          )}
          {sessions.data && sessions.data.total > 5 ? (
            <Link
              className="tree-more"
              to={`/projects/${encodeURIComponent(project.id)}/sessions`}
            >
              查看全部
            </Link>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

interface UnboundTreeNodeProps {
  activeSessionId: string | null;
}

function UnboundTreeNode({ activeSessionId }: UnboundTreeNodeProps) {
  const [expanded, setExpanded] = useState(false);
  const sessions = useSessionList(
    { page: 1, pageSize: 5, unbound: true },
    expanded,
  );
  const regionId = "project-tree-unbound";

  return (
    <div className={`tree-node tree-node--unbound${expanded ? " is-open" : ""}`}>
      <button
        aria-controls={regionId}
        aria-expanded={expanded}
        aria-label="未归属项目"
        className="tree-project-toggle"
        onClick={() => setExpanded((value) => !value)}
        type="button"
      >
        <Icon name="inbox" size={14} />
        <span>未归属项目</span>
        <Icon className="tree-chevron" name="chevron" size={12} />
      </button>
      {expanded ? (
        <div className="tree-sessions" id={regionId}>
          {sessions.isPending ? (
            <p className="tree-state">正在加载会话…</p>
          ) : sessions.isError ? (
            <p className="tree-state tree-state--error" role="alert">
              会话加载失败
            </p>
          ) : sessions.data.items.length === 0 ? (
            <p className="tree-state">暂无会话</p>
          ) : (
            sessions.data.items.map((session) => (
              <SessionLink
                activeSessionId={activeSessionId}
                key={session.id}
                session={session}
              />
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}

export function ChatSidebarContent() {
  const location = useLocation();
  const { projectId: draftProjectId } = useDraftChat();
  const projects = useProjectDirectory();
  const recent = useSessionList({ page: 1, pageSize: 8 });
  const unboundSummary = useSessionList({
    page: 1,
    pageSize: 1,
    unbound: true,
  });
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [recentExpanded, setRecentExpanded] = useState(readRecentExpanded);
  const activeSessionId = activeSessionIdFromPath(location.pathname);
  const activeSession = useSession(activeSessionId);
  const isDraftRoute =
    location.pathname === "/chat" || location.pathname === "/chat/new";
  const activeProjectId = activeSession.data?.project_id ?? null;
  const contextProjectId =
    activeProjectId ?? (isDraftRoute ? draftProjectId : null);

  useEffect(() => {
    try {
      // 最近列表折叠状态只保存在当前浏览器，跨会话导航与刷新后继续生效。
      window.localStorage.setItem(
        RECENT_SECTION_STORAGE_KEY,
        recentExpanded ? "expanded" : "collapsed",
      );
    } catch {
      // 浏览器禁用本地存储时保留当前内存状态，不影响侧栏操作。
    }
  }, [recentExpanded]);

  const projectNames = new Map(
    (projects.data ?? []).map((project) => [project.id, project.name]),
  );

  return (
    <>
      {/* 新建对话即使目标 URL 相同也要新增历史项，旧草稿保留给 POP 恢复。 */}
      <Link
        className="sidebar-new-chat"
        replace={false}
        to={newChatHref(contextProjectId)}
      >
        <Icon name="edit" size={15} />
        <span>新建对话</span>
      </Link>

      <div className="sidebar-scroll">
        <section className="sidebar-section" aria-labelledby="project-tree-title">
          <div className="sidebar-section-title-row">
            <span id="project-tree-title">项目</span>
            <button
              aria-label="新建项目"
              className="sidebar-add-button"
              onClick={() => setNewProjectOpen(true)}
              title="新建项目"
              type="button"
            >
              <Icon name="plus" size={13} />
            </button>
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
            <div className="project-tree">
              {projects.data.map((project) => (
                <ProjectTreeNode
                  activeProjectId={activeProjectId}
                  activeSessionId={activeSessionId}
                  key={project.id}
                  project={project}
                />
              ))}
            </div>
          )}
          {unboundSummary.data && unboundSummary.data.total > 0 ? (
            <div className="project-tree project-tree--unbound">
              <UnboundTreeNode activeSessionId={activeSessionId} />
            </div>
          ) : null}
        </section>

        <section className="sidebar-section" aria-labelledby="recent-title">
          <div className="sidebar-section-title-row">
            <button
              aria-controls="recent-sessions-content"
              aria-expanded={recentExpanded}
              className="sidebar-section-toggle"
              onClick={() => setRecentExpanded((expanded) => !expanded)}
              type="button"
            >
              <span id="recent-title">最近</span>
              <Icon
                className={`tree-chevron${recentExpanded ? " is-open" : ""}`}
                name="chevron"
                size={12}
              />
            </button>
          </div>
          <div hidden={!recentExpanded} id="recent-sessions-content">
              {recent.isPending ? (
                <p className="tree-state">正在加载最近会话…</p>
              ) : recent.isError ? (
                <p className="tree-state tree-state--error" role="alert">
                  最近会话加载失败
                </p>
              ) : recent.data.items.length === 0 ? (
                <p className="tree-state">还没有对话记录</p>
              ) : (
                <div className="recent-list">
                  {recent.data.items.slice(0, 8).map((session) => (
                    <Link
                      aria-current={
                        activeSessionId === session.id ? "page" : undefined
                      }
                      className={`recent-session${
                        activeSessionId === session.id ? " is-active" : ""
                      }`}
                      key={session.id}
                      to={`/chat/${encodeURIComponent(session.id)}`}
                    >
                      <strong>{sessionDisplayTitle(session.title)}</strong>
                      <span>
                        {session.project_id
                          ? projectNames.get(session.project_id) ?? "项目不可访问"
                          : "未归属项目"}{" "}
                        · {formatShortDateTime(session.updated_at)}
                      </span>
                    </Link>
                  ))}
                </div>
              )}
          </div>
        </section>
      </div>

      <NewProjectDialog
        onClose={() => setNewProjectOpen(false)}
        open={newProjectOpen}
      />
    </>
  );
}

