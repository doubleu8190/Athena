import { Link } from "react-router-dom";

import { formatDateTime, formatShortDateTime } from "../../shared/lib/format";
import { Icon } from "../../shared/ui/Icons";
import { newChatHref } from "../chat/draft";
import { useSessionList } from "../sessions/hooks";
import { sessionDisplayTitle } from "../sessions/model";
import type { Project } from "./model";

interface ProjectOverviewTabProps {
  project: Project;
}

export function ProjectOverviewTab({ project }: ProjectOverviewTabProps) {
  const sessions = useSessionList({
    page: 1,
    pageSize: 5,
    projectId: project.id,
  });

  return (
    <div className="project-tab-content">
      <section className="stat-strip" aria-label="项目概览">
        <div className="stat-cell">
          <span className="stat-label">项目会话</span>
          <strong className="stat-value">
            {sessions.data?.total ?? "—"}
          </strong>
          <small>真实会话总数</small>
        </div>
        <div className="stat-cell">
          <span className="stat-label">项目创建</span>
          <strong className="stat-value stat-value--date">
            {formatDateTime(project.created_at)}
          </strong>
          <small>创建时间</small>
        </div>
        <div className="stat-cell">
          <span className="stat-label">最近更新</span>
          <strong className="stat-value stat-value--date">
            {formatDateTime(project.updated_at)}
          </strong>
          <small>项目名称最近变更</small>
        </div>
      </section>

      <section className="surface-card">
        <div className="surface-header">
          <div className="surface-title">
            <strong>最近会话</strong>
            <span>按后端更新时间排列</span>
          </div>
          <Link
            className="surface-link"
            to={`/projects/${encodeURIComponent(project.id)}/sessions`}
          >
            查看全部
            <Icon name="arrow-right" size={12} />
          </Link>
        </div>

        {sessions.isPending ? (
          <p className="surface-state">正在加载会话…</p>
        ) : sessions.isError ? (
          <p className="surface-state surface-state--error" role="alert">
            最近会话加载失败。
          </p>
        ) : sessions.data.items.length === 0 ? (
          <div className="surface-empty">
            <p>这个项目还没有会话。</p>
            <Link className="button-primary" to={newChatHref(project.id)}>
              开始对话
            </Link>
          </div>
        ) : (
          <div className="session-list">
            {sessions.data.items.map((session) => (
              <Link
                className="project-session-link"
                key={session.id}
                to={`/chat/${encodeURIComponent(session.id)}`}
              >
                <span className="session-row-icon">
                  <Icon name="chat" size={15} />
                </span>
                <span className="project-session-link-copy">
                  <strong>{sessionDisplayTitle(session.title)}</strong>
                  <span>更新于 {formatShortDateTime(session.updated_at)}</span>
                </span>
              </Link>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

