import {
  ArrowRight,
  ChevronRight,
  Folder,
  Pencil,
  Trash2,
} from "lucide-react";
import { useState } from "react";
import { Link, Navigate, useLocation, useParams } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import {
  Button,
  EmptyState,
  InlineAlert,
  Skeleton,
} from "../../shared/ui";
import { newChatHref } from "../chat/draft";
import { DeleteProjectDialog } from "./DeleteProjectDialog";
import { isProjectNotFound, projectErrorMessage } from "./errorMessages";
import { useProject } from "./hooks";
import type { Project } from "./model";
import { ProjectConfigPanel } from "./ProjectConfigPanel";
import { EditProjectInfoDialog } from "./ProjectNameDialogs";
import { ProjectSessionsPanel } from "./ProjectSessionsPanel";

/**
 * 兼容旧版 4-Tab 项目路由（/projects/:projectId/:tab）：
 * 温和重定向到新的单页工作文件夹 /projects/:projectId，并完整保留查询参数（如 ?page=2）。
 */
export function LegacyProjectTabRedirect() {
  const { projectId } = useParams<{ projectId: string }>();
  const location = useLocation();
  if (!projectId) {
    return <Navigate replace to="/projects" />;
  }
  return (
    <Navigate
      replace
      to={`/projects/${encodeURIComponent(projectId)}${location.search}`}
    />
  );
}

interface ProjectWorkspaceContentProps {
  project: Project;
}

function ProjectWorkspaceContent({ project }: ProjectWorkspaceContentProps) {
  const [editInfoOpen, setEditInfoOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 py-6 sm:px-6 lg:px-8">
      {/* 顶部面包屑与项目页头 */}
      <header className="space-y-3 border-b border-border-subtle pb-5">
        <nav
          aria-label="项目面包屑"
          className="flex items-center gap-1.5 text-xs text-text-tertiary"
        >
          <Link
            className="inline-flex items-center gap-1 rounded hover:text-text-primary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring-focus/40"
            to="/projects"
          >
            <Folder aria-hidden="true" className="h-3.5 w-3.5" />
            <span>项目</span>
          </Link>
          <ChevronRight aria-hidden="true" className="h-3.5 w-3.5" />
          <span className="truncate font-medium text-text-secondary">
            {project.name}
          </span>
        </nav>

        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0 flex-1 space-y-1.5">
            <h1 className="break-words text-xl font-semibold tracking-tight text-text-primary">
              {project.name}
            </h1>
            {project.description?.trim() ? (
              <p className="max-w-3xl text-sm leading-relaxed text-text-secondary">
                {project.description}
              </p>
            ) : null}
            <p className="text-xs text-text-tertiary">
              创建于 {formatDateTime(project.created_at)} · 更新于{" "}
              {formatDateTime(project.updated_at)}
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              onClick={() => setEditInfoOpen(true)}
              size="sm"
              type="button"
              variant="outline"
            >
              <Pencil aria-hidden="true" className="h-3.5 w-3.5" />
              编辑信息
            </Button>
            <Button
              onClick={() => setDeleteOpen(true)}
              size="sm"
              type="button"
              variant="danger-outline"
            >
              <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
              删除项目
            </Button>
            <Button asChild size="sm" variant="primary">
              <Link to={newChatHref(project.id)}>
                <span>开始对话</span>
                <ArrowRight aria-hidden="true" className="h-4 w-4" />
              </Link>
            </Button>
          </div>
        </div>
      </header>

      {/* 单页双栏工作文件夹：宽屏左侧对话流 + 右侧固定比例配置栏，<1024px 自动纵向堆叠 */}
      <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-[minmax(0,1fr)_360px] xl:grid-cols-[minmax(0,1fr)_380px]">
        <ProjectSessionsPanel project={project} />
        <ProjectConfigPanel project={project} />
      </div>

      <EditProjectInfoDialog
        onClose={() => setEditInfoOpen(false)}
        open={editInfoOpen}
        project={project}
      />
      <DeleteProjectDialog
        onClose={() => setDeleteOpen(false)}
        open={deleteOpen}
        project={project}
      />
    </div>
  );
}

/** 单页个人项目工作文件夹（/projects/:projectId）。 */
export function ProjectWorkspacePage() {
  const { projectId } = useParams<{ projectId: string }>();
  const project = useProject(projectId);

  if (!projectId) {
    return <Navigate replace to="/projects" />;
  }

  if (project.isPending) {
    return (
      <div
        className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 py-6 sm:px-6 lg:px-8"
        role="status"
      >
        <span className="sr-only">正在加载项目…</span>
        <Skeleton className="h-20 w-full rounded-xl" />
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
          <Skeleton className="h-72 w-full rounded-xl" />
          <Skeleton className="h-72 w-full rounded-xl" />
        </div>
      </div>
    );
  }

  if (project.isError) {
    const notFound = isProjectNotFound(project.error);
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-12 sm:px-6">
        <EmptyState
          action={
            <Button asChild size="sm" variant="primary">
              <Link to="/projects">返回项目中心</Link>
            </Button>
          }
          description={
            notFound
              ? "该项目不存在、已删除，或不属于当前账号。"
              : "项目信息暂时无法加载，请稍后重试。"
          }
          icon="folder"
          title={notFound ? "项目不存在或不可访问" : "项目加载失败"}
        />
        {!notFound ? (
          <InlineAlert
            action={
              <Button
                onClick={() => void project.refetch()}
                size="xs"
                type="button"
                variant="outline"
              >
                重试
              </Button>
            }
            description={projectErrorMessage(project.error)}
            variant="danger"
          />
        ) : null}
      </div>
    );
  }

  // 跨项目切换时以 project.id 隔离工作区实例，重置左栏会话搜索防抖与右栏编辑/弹窗状态。
  return (
    <ProjectWorkspaceContent key={project.data.id} project={project.data} />
  );
}
