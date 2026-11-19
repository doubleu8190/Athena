import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { newChatHref } from "../chat/draft";
import { sessionErrorMessage } from "../sessions/errorMessages";
import { useSessionList } from "../sessions/hooks";
import {
  sessionDisplayTitle,
  type Session,
} from "../sessions/model";
import {
  SessionDeleteDialog,
  SessionRenameDialog,
} from "../sessions/SessionDialogs";
import type { Project } from "./model";

const PAGE_SIZE = 10;

interface ProjectSessionsTabProps {
  project: Project;
}

function parsePage(value: string | null): number {
  if (!value) {
    return 1;
  }
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 1;
}

export function ProjectSessionsTab({ project }: ProjectSessionsTabProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const page = parsePage(searchParams.get("page"));
  const sessions = useSessionList({
    page,
    pageSize: PAGE_SIZE,
    projectId: project.id,
  });
  const [renameSession, setRenameSession] = useState<Session | null>(null);
  const [deleteSession, setDeleteSession] = useState<Session | null>(null);
  const [deleteLastOnPage, setDeleteLastOnPage] = useState(false);
  const totalPages = Math.max(
    1,
    Math.ceil((sessions.data?.total ?? 0) / PAGE_SIZE),
  );

  useEffect(() => {
    if (sessions.data && page > totalPages) {
      setSearchParams(
        totalPages > 1 ? { page: String(totalPages) } : {},
        { replace: true },
      );
    }
  }, [page, sessions.data, setSearchParams, totalPages]);

  const goToPage = (nextPage: number) => {
    setSearchParams(
      nextPage > 1 ? { page: String(nextPage) } : {},
      { replace: true },
    );
  };

  const handleDeleted = () => {
    if (deleteLastOnPage && page > 1) {
      goToPage(page - 1);
    }
    setDeleteLastOnPage(false);
    setDeleteSession(null);
  };

  return (
    <div className="project-tab-content">
      <header className="project-panel-heading">
        <div>
          <p className="page-eyebrow">Project sessions</p>
          <h2>会话</h2>
          <p>只展示后端真实字段，并按最近更新时间排列。</p>
        </div>
        <Link className="button-primary" to={newChatHref(project.id)}>
          <Icon name="plus" size={14} />
          新建对话
        </Link>
      </header>

      {sessions.isPending ? (
        <div className="page-loading" role="status">
          正在加载会话…
        </div>
      ) : sessions.isError ? (
        <ErrorBanner
          message={sessionErrorMessage(sessions.error)}
          onRetry={() => void sessions.refetch()}
        />
      ) : sessions.data.items.length === 0 ? (
        <EmptyState
          action={
            <Link className="button-primary" to={newChatHref(project.id)}>
              新建对话
            </Link>
          }
          description="这个项目还没有会话。新建对话后，项目绑定会在发送首条消息时锁定。"
          icon="chat"
          title="还没有项目会话"
        />
      ) : (
        <section className="surface-card project-session-table" aria-label="项目会话列表">
          <div className="project-table-head" aria-hidden="true">
            <span>会话</span>
            <span>创建时间</span>
            <span>更新时间</span>
            <span>操作</span>
          </div>
          {sessions.data.items.map((session) => (
            <article className="project-session-row" key={session.id}>
              <Link
                className="project-session-main"
                to={`/chat/${encodeURIComponent(session.id)}`}
              >
                <span className="session-row-icon">
                  <Icon name="chat" size={15} />
                </span>
                <strong>{sessionDisplayTitle(session.title)}</strong>
              </Link>
              {/* 为时间戳提供 data-label，便于移动端隐藏表头时通过伪元素展示语义前缀 */}
              <time
                className="project-session-date"
                data-label="创建于 "
                dateTime={session.created_at}
              >
                {formatDateTime(session.created_at)}
              </time>
              <time
                className="project-session-date"
                data-label="更新于 "
                dateTime={session.updated_at}
              >
                {formatDateTime(session.updated_at)}
              </time>
              <div className="session-actions">
                <button
                  aria-label={`重命名会话：${sessionDisplayTitle(
                    session.title,
                  )}`}
                  className="icon-button"
                  onClick={() => setRenameSession(session)}
                  title="重命名会话"
                  type="button"
                >
                  <Icon name="edit" size={15} />
                </button>
                <button
                  aria-label={`删除会话：${sessionDisplayTitle(
                    session.title,
                  )}`}
                  className="icon-button icon-button--danger"
                  onClick={() => {
                    setDeleteLastOnPage(
                      sessions.data?.items.length === 1 && page > 1,
                    );
                    setDeleteSession(session);
                  }}
                  title="删除会话"
                  type="button"
                >
                  <Icon name="trash" size={15} />
                </button>
              </div>
            </article>
          ))}
          <footer className="project-list-footer">
            <span>共 {sessions.data.total} 个会话</span>
            <nav className="project-pagination" aria-label="会话分页">
              <button
                aria-label="上一页"
                className="project-page-button"
                disabled={page <= 1}
                onClick={() => goToPage(page - 1)}
                type="button"
              >
                <Icon name="chevron" size={13} />
              </button>
              <span className="project-page-status">
                第 {page} / {totalPages} 页
              </span>
              <button
                aria-label="下一页"
                className="project-page-button project-page-button--next"
                disabled={page >= totalPages}
                onClick={() => goToPage(page + 1)}
                type="button"
              >
                <Icon name="chevron" size={13} />
              </button>
            </nav>
          </footer>
        </section>
      )}

      {renameSession ? (
        <SessionRenameDialog
          onClose={() => setRenameSession(null)}
          open
          session={renameSession}
        />
      ) : null}
      {deleteSession ? (
        <SessionDeleteDialog
          onClose={() => setDeleteSession(null)}
          onDeleted={handleDeleted}
          open
          session={deleteSession}
        />
      ) : null}
    </div>
  );
}

