import {
  BookMarked,
  BookOpen,
  Bot,
  ChevronRight,
  FileText,
  Folder,
  Plug,
  Plus,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Wrench,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";

import { useAuth } from "../../features/auth/AuthProvider";
import { useProjectDirectory } from "../../features/projects/hooks";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
  Skeleton,
  cn,
} from "../../shared/ui";
import {
  activeProjectIdFromPath,
  getAdminNavigationSection,
  isNavTargetActive,
  type ShellAdminIconKey,
} from "./navigation";

const MAX_SIDEBAR_PROJECTS = 5;

function AdminItemIcon({ icon }: { icon: ShellAdminIconKey }) {
  const className = "h-4 w-4 shrink-0";
  switch (icon) {
    case "models":
      return <SlidersHorizontal aria-hidden="true" className={className} />;
    case "prompts":
      return <FileText aria-hidden="true" className={className} />;
    case "mcp":
      return <Plug aria-hidden="true" className={className} />;
    case "tools":
      return <Wrench aria-hidden="true" className={className} />;
    case "skills":
      return <Sparkles aria-hidden="true" className={className} />;
    case "subagents":
      return <Bot aria-hidden="true" className={className} />;
    case "dept-knowledge":
      return <BookMarked aria-hidden="true" className={className} />;
  }
}

interface SidebarWorkspaceNavProps {
  onOpenNewProject: () => void;
}

/** 展开态侧栏下方的工作台模块导航：可折叠项目列表、知识库/技能库与按角色动态呈现的管理分区。 */
export function SidebarWorkspaceNav({
  onOpenNewProject,
}: SidebarWorkspaceNavProps) {
  const { skillAdminAccessDenied, superAdminAccessDenied, user } = useAuth();
  const location = useLocation();
  const projects = useProjectDirectory();
  const [projectsOpen, setProjectsOpen] = useState(true);

  const isAdminPath = location.pathname.startsWith("/admin");
  const [adminOpen, setAdminOpen] = useState(isAdminPath);

  useEffect(() => {
    // 超级管理员进入任意 /admin/* 页面时自动展开管理后台分区，保持当前页导航可见。
    if (isAdminPath) {
      setAdminOpen(true);
    }
  }, [isAdminPath]);

  const adminSection = getAdminNavigationSection(user, {
    skillAdminAccessDenied,
    superAdminAccessDenied,
  });

  const activeProjectId = activeProjectIdFromPath(location.pathname);
  const visibilityScope = new URLSearchParams(location.search).get(
    "visibility_scope",
  );
  const isDeptKnowledgeActive =
    adminSection?.kind === "dept_admin" &&
    location.pathname.startsWith("/knowledge") &&
    visibilityScope === "department";
  const isKnowledgeActive =
    location.pathname.startsWith("/knowledge") && !isDeptKnowledgeActive;
  const isSkillsActive = location.pathname.startsWith("/skills");
  const isAllProjectsActive = location.pathname === "/projects";

  const recentProjects = (projects.data ?? []).slice(0, MAX_SIDEBAR_PROJECTS);

  return (
    <nav
      aria-label="主导航"
      className="min-h-0 max-h-[45%] shrink space-y-1 overflow-y-auto border-t border-border-subtle px-2 py-2"
    >
      {/* 项目可折叠分区：展示最近 5 个项目 + 快捷新建按钮 + 查看全部项目 */}
      <Collapsible onOpenChange={setProjectsOpen} open={projectsOpen}>
        <div className="flex items-center justify-between gap-1">
          <CollapsibleTrigger asChild>
            <button
              className={cn(
                "flex min-w-0 flex-1 items-center gap-2 rounded-lg px-2 py-1.5 text-[13px] font-medium transition-colors",
                "text-text-secondary hover:bg-muted/80 hover:text-text-primary",
                "focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring-focus",
              )}
              type="button"
            >
              <ChevronRight
                aria-hidden="true"
                className={cn(
                  "h-3.5 w-3.5 shrink-0 text-text-tertiary transition-transform duration-150",
                  projectsOpen && "rotate-90",
                )}
              />
              <Folder aria-hidden="true" className="h-4 w-4 shrink-0" />
              <span className="truncate">项目</span>
            </button>
          </CollapsibleTrigger>
          <button
            aria-label="新建项目"
            className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-text-tertiary transition-colors hover:bg-muted hover:text-text-primary focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring-focus"
            data-compact-control="true"
            onClick={onOpenNewProject}
            title="新建项目"
            type="button"
          >
            <Plus aria-hidden="true" className="h-3.5 w-3.5" />
          </button>
        </div>

        <CollapsibleContent className="mt-0.5 space-y-0.5 pl-4">
          {projects.isPending ? (
            <div className="space-y-1 px-2 py-1" role="status">
              <span className="sr-only">正在加载项目…</span>
              <Skeleton className="h-6 w-4/5 rounded-md" />
              <Skeleton className="h-6 w-3/5 rounded-md" />
            </div>
          ) : projects.isError ? (
            <p className="px-2 py-1 text-xs text-text-tertiary">项目加载失败</p>
          ) : recentProjects.length === 0 ? (
            <p className="px-2 py-1 text-xs text-text-tertiary">还没有项目</p>
          ) : (
            recentProjects.map((project) => {
              const isActive = activeProjectId === project.id;
              return (
                <Link
                  aria-current={isActive ? "page" : undefined}
                  className={cn(
                    "relative flex items-center rounded-md px-2.5 py-1 text-[13px] transition-colors",
                    "focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring-focus",
                    isActive
                      ? "bg-active font-medium text-brand"
                      : "text-text-secondary hover:bg-muted/80 hover:text-text-primary",
                  )}
                  key={project.id}
                  title={project.name}
                  to={`/projects/${encodeURIComponent(project.id)}/overview`}
                >
                  {isActive ? (
                    <span
                      aria-hidden="true"
                      className="absolute top-1 bottom-1 left-0 w-[3px] rounded-full bg-brand"
                    />
                  ) : null}
                  <span className="truncate">{project.name}</span>
                </Link>
              );
            })
          )}
          <Link
            aria-current={isAllProjectsActive ? "page" : undefined}
            className={cn(
              "flex items-center rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
              "focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring-focus",
              isAllProjectsActive
                ? "bg-active text-brand"
                : "text-brand hover:bg-brand-subtle/70",
            )}
            to="/projects"
          >
            查看全部项目 →
          </Link>
        </CollapsibleContent>
      </Collapsible>

      {/* 一级业务模块入口 */}
      <Link
        aria-current={isKnowledgeActive ? "page" : undefined}
        className={cn(
          "relative flex items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-[13px] transition-colors",
          "focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring-focus",
          isKnowledgeActive
            ? "bg-active font-medium text-brand"
            : "text-text-secondary hover:bg-muted/80 hover:text-text-primary",
        )}
        to="/knowledge"
      >
        {isKnowledgeActive ? (
          <span
            aria-hidden="true"
            className="absolute top-1.5 bottom-1.5 left-0 w-[3px] rounded-full bg-brand"
          />
        ) : null}
        <BookOpen aria-hidden="true" className="h-4 w-4 shrink-0" />
        <span className="truncate">知识库</span>
      </Link>

      <Link
        aria-current={isSkillsActive ? "page" : undefined}
        className={cn(
          "relative flex items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-[13px] transition-colors",
          "focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring-focus",
          isSkillsActive
            ? "bg-active font-medium text-brand"
            : "text-text-secondary hover:bg-muted/80 hover:text-text-primary",
        )}
        to="/skills"
      >
        {isSkillsActive ? (
          <span
            aria-hidden="true"
            className="absolute top-1.5 bottom-1.5 left-0 w-[3px] rounded-full bg-brand"
          />
        ) : null}
        <Sparkles aria-hidden="true" className="h-4 w-4 shrink-0" />
        <span className="truncate">技能库</span>
      </Link>

      {/* 角色动态管理分区：普通员工完全不渲染 */}
      {adminSection ? (
        <div className="pt-1.5">
          <div className="mb-1.5 border-t border-border-subtle" />
          {adminSection.collapsible ? (
            <Collapsible onOpenChange={setAdminOpen} open={adminOpen}>
              <CollapsibleTrigger asChild>
                <button
                  className={cn(
                    "flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-[13px] font-medium transition-colors",
                    isAdminPath
                      ? "text-brand"
                      : "text-text-secondary hover:bg-muted/80 hover:text-text-primary",
                    "focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring-focus",
                  )}
                  type="button"
                >
                  <ChevronRight
                    aria-hidden="true"
                    className={cn(
                      "h-3.5 w-3.5 shrink-0 text-text-tertiary transition-transform duration-150",
                      adminOpen && "rotate-90",
                    )}
                  />
                  <ShieldCheck aria-hidden="true" className="h-4 w-4 shrink-0" />
                  <span className="truncate">{adminSection.title}</span>
                </button>
              </CollapsibleTrigger>
              <CollapsibleContent className="mt-0.5 space-y-0.5 pl-4">
                <div aria-label={adminSection.title} role="group">
                  {adminSection.items.map((item) => {
                    const active = isNavTargetActive(
                      item.to,
                      location.pathname,
                      location.search,
                    );
                    return (
                      <Link
                        aria-current={active ? "page" : undefined}
                        className={cn(
                          "relative flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] transition-colors",
                          "focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring-focus",
                          active
                            ? "bg-active font-medium text-brand"
                            : "text-text-secondary hover:bg-muted/80 hover:text-text-primary",
                        )}
                        key={item.to}
                        to={item.to}
                      >
                        {active ? (
                          <span
                            aria-hidden="true"
                            className="absolute top-1.5 bottom-1.5 left-0 w-[3px] rounded-full bg-brand"
                          />
                        ) : null}
                        <AdminItemIcon icon={item.icon} />
                        <span className="truncate">{item.label}</span>
                      </Link>
                    );
                  })}
                </div>
              </CollapsibleContent>
            </Collapsible>
          ) : (
            <div aria-label={adminSection.title} className="space-y-0.5" role="group">
              <div className="px-2.5 py-1 text-[11px] font-medium text-text-tertiary">
                {adminSection.title}
              </div>
              {adminSection.items.map((item) => {
                const active = isNavTargetActive(
                  item.to,
                  location.pathname,
                  location.search,
                );
                return (
                  <Link
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "relative flex items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-[13px] transition-colors",
                      "focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring-focus",
                      active
                        ? "bg-active font-medium text-brand"
                        : "text-text-secondary hover:bg-muted/80 hover:text-text-primary",
                    )}
                    key={item.to}
                    to={item.to}
                  >
                    {active ? (
                      <span
                        aria-hidden="true"
                        className="absolute top-1.5 bottom-1.5 left-0 w-[3px] rounded-full bg-brand"
                      />
                    ) : null}
                    <AdminItemIcon icon={item.icon} />
                    <span className="truncate">{item.label}</span>
                  </Link>
                );
              })}
            </div>
          )}
        </div>
      ) : null}
    </nav>
  );
}
