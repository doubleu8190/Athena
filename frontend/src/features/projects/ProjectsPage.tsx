import {
  FileText,
  Folder,
  MoreHorizontal,
  Pencil,
  Plus,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import {
  Badge,
  Button,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  EmptyState,
  InlineAlert,
  Skeleton,
} from "../../shared/ui";
import { DeleteProjectDialog } from "./DeleteProjectDialog";
import { projectErrorMessage } from "./errorMessages";
import { useProjectDirectory } from "./hooks";
import type { Project } from "./model";
import { NewProjectDialog, RenameProjectDialog } from "./ProjectNameDialogs";

/** 个人项目工作文件夹目录页（/projects）：响应式 3 列卡片网格与快捷操作菜单。 */
export function ProjectsPage() {
  const projects = useProjectDirectory();
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<Project | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null);

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 py-6 sm:px-6 lg:px-8">
      <header className="flex flex-wrap items-start justify-between gap-4 border-b border-border-subtle pb-5">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold tracking-tight text-text-primary">
            项目
          </h1>
          <p className="text-sm text-text-secondary">
            将同一主题的对话、专属背景指令、默认技能与知识库整理在独立工作文件夹中。
          </p>
        </div>
        <Button
          onClick={() => setNewProjectOpen(true)}
          size="sm"
          type="button"
          variant="primary"
        >
          <Plus aria-hidden="true" className="h-4 w-4" />
          新建项目
        </Button>
      </header>

      {projects.isPending ? (
        <div
          className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3"
          role="status"
        >
          <span className="sr-only">正在加载项目…</span>
          <Skeleton className="h-40 w-full rounded-xl" />
          <Skeleton className="h-40 w-full rounded-xl" />
          <Skeleton className="h-40 w-full rounded-xl" />
        </div>
      ) : projects.isError ? (
        <InlineAlert
          action={
            <Button
              onClick={() => void projects.refetch()}
              size="xs"
              type="button"
              variant="outline"
            >
              重试
            </Button>
          }
          description={projectErrorMessage(projects.error)}
          title="项目列表加载失败"
          variant="danger"
        />
      ) : projects.data.length === 0 ? (
        <EmptyState
          action={
            <Button
              onClick={() => setNewProjectOpen(true)}
              size="sm"
              type="button"
              variant="primary"
            >
              <Plus aria-hidden="true" className="h-4 w-4" />
              新建项目
            </Button>
          }
          description="创建第一个项目文件夹，统一管理该主题的对话、专属背景指令、默认技能与知识库。"
          icon="folder"
          title="还没有项目"
        />
      ) : (
        <section aria-labelledby="project-list-title" className="space-y-3">
          <div className="flex items-center justify-between gap-2 text-xs text-text-tertiary">
            <h2
              className="text-sm font-medium text-text-secondary"
              id="project-list-title"
            >
              全部项目（{projects.data.length}）
            </h2>
            <span>按最近更新时间排列</span>
          </div>

          <ul className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            {projects.data.map((project) => {
              const hasInstructions = Boolean(project.instructions?.trim());
              const defaultSkillsCount = project.default_skill_ids.length;
              return (
                <li
                  className="group relative flex flex-col justify-between rounded-xl border border-border-subtle bg-canvas p-4 shadow-xs transition-colors hover:border-border-strong"
                  key={project.id}
                >
                  <div className="space-y-3">
                    <div className="flex items-start justify-between gap-2">
                      <Link
                        className="flex min-w-0 flex-1 items-center gap-2.5 rounded-md focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring-focus/40"
                        to={`/projects/${encodeURIComponent(project.id)}`}
                      >
                        <span
                          aria-hidden="true"
                          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-subtle text-brand"
                        >
                          <Folder className="h-4 w-4" />
                        </span>
                        <span className="truncate text-sm font-semibold text-text-primary group-hover:text-brand">
                          {project.name}
                        </span>
                      </Link>

                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button
                            aria-label={`更多项目操作：${project.name}`}
                            data-icon-button="true"
                            size="icon"
                            type="button"
                            variant="ghost"
                          >
                            <MoreHorizontal
                              aria-hidden="true"
                              className="h-4 w-4"
                            />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          <DropdownMenuItem
                            onSelect={() => setRenameTarget(project)}
                          >
                            <Pencil
                              aria-hidden="true"
                              className="h-3.5 w-3.5"
                            />
                            <span>重命名</span>
                          </DropdownMenuItem>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem
                            destructive
                            onSelect={() => setDeleteTarget(project)}
                          >
                            <Trash2
                              aria-hidden="true"
                              className="h-3.5 w-3.5"
                            />
                            <span>删除项目</span>
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </div>

                    {project.description?.trim() ? (
                      <p className="line-clamp-2 text-xs leading-relaxed text-text-secondary">
                        {project.description}
                      </p>
                    ) : (
                      <p className="text-xs text-text-tertiary">暂无项目简介</p>
                    )}

                    {hasInstructions || defaultSkillsCount > 0 ? (
                      <div className="flex flex-wrap items-center gap-1.5">
                        {hasInstructions ? (
                          <Badge variant="brand">
                            <FileText
                              aria-hidden="true"
                              className="h-3 w-3"
                            />
                            已配置专属指令
                          </Badge>
                        ) : null}
                        {defaultSkillsCount > 0 ? (
                          <Badge variant="info">
                            <Sparkles
                              aria-hidden="true"
                              className="h-3 w-3"
                            />
                            默认技能 {defaultSkillsCount}
                          </Badge>
                        ) : null}
                      </div>
                    ) : null}
                  </div>

                  <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-border-subtle pt-3 text-[11px] text-text-tertiary">
                    <span>创建于 {formatDateTime(project.created_at)}</span>
                    <span>更新于 {formatDateTime(project.updated_at)}</span>
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      <NewProjectDialog
        onClose={() => setNewProjectOpen(false)}
        open={newProjectOpen}
      />
      {renameTarget ? (
        <RenameProjectDialog
          onClose={() => setRenameTarget(null)}
          open
          project={renameTarget}
        />
      ) : null}
      {deleteTarget ? (
        <DeleteProjectDialog
          onClose={() => setDeleteTarget(null)}
          onDeleted={() => setDeleteTarget(null)}
          open
          project={deleteTarget}
        />
      ) : null}
    </div>
  );
}
