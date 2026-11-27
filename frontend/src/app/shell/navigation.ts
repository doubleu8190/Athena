import {
  canManageSkills,
  isSuperAdmin,
  type User,
  type UserRole,
} from "../../features/auth/model";
import { DEFAULT_DESKTOP_BREAKPOINT_PX } from "../../shared/lib/viewport";
import type { BadgeProps } from "../../shared/ui/Badge";

export const SIDEBAR_COLLAPSED_STORAGE_KEY = "athena.shell.sidebar-collapsed";
export const DESKTOP_BREAKPOINT_PX = DEFAULT_DESKTOP_BREAKPOINT_PX;

export type ShellAdminIconKey =
  | "models"
  | "prompts"
  | "mcp"
  | "tools"
  | "skills"
  | "subagents"
  | "dept-knowledge";

export interface ShellAdminNavItem {
  icon: ShellAdminIconKey;
  label: string;
  to: string;
}

export interface AdminNavigationSection {
  collapsedLabel: string;
  collapsedTarget: string;
  collapsible: boolean;
  items: ShellAdminNavItem[];
  kind: "super_admin" | "dept_admin";
  title: string;
}

/** 超级管理员完整 6 项企业级后台导航入口。 */
export const SUPER_ADMIN_NAVIGATION: ShellAdminNavItem[] = [
  { icon: "models", label: "模型与压缩策略", to: "/admin/models" },
  { icon: "prompts", label: "系统提示词", to: "/admin/system-prompts" },
  { icon: "mcp", label: "MCP 服务", to: "/admin/mcp" },
  { icon: "tools", label: "工具审批策略", to: "/admin/tools" },
  { icon: "skills", label: "企业与部门技能", to: "/admin/skills" },
  { icon: "subagents", label: "子智能体", to: "/admin/subagents" },
];

/** 部门管理员仅展示其管辖范围内的技能与部门知识库入口。 */
export const DEPT_ADMIN_NAVIGATION: ShellAdminNavItem[] = [
  { icon: "skills", label: "部门技能", to: "/admin/skills" },
  {
    icon: "dept-knowledge",
    label: "部门知识库",
    to: "/knowledge?scope=department",
  },
];

interface AdminAccessFlags {
  skillAdminAccessDenied?: boolean;
  superAdminAccessDenied?: boolean;
}

/**
 * 严格对齐一期三层权限体系生成侧栏动态管理分区：
 * - super_admin（未被拒止）：渲染「管理后台」6 项完整入口；
 * - dept_admin（具备技能管理权限且未被拒止）：渲染「部门管理」2 项入口；
 * - member 或权限已被拒止：返回 null，完全不渲染管理分区。
 */
export function getAdminNavigationSection(
  user: User | null | undefined,
  flags: AdminAccessFlags = {},
): AdminNavigationSection | null {
  if (!user) {
    return null;
  }
  if (isSuperAdmin(user) && !flags.superAdminAccessDenied) {
    return {
      collapsedLabel: "管理后台",
      collapsedTarget: "/admin/models",
      collapsible: true,
      items: SUPER_ADMIN_NAVIGATION,
      kind: "super_admin",
      title: "管理后台",
    };
  }
  if (
    !isSuperAdmin(user) &&
    canManageSkills(user) &&
    !flags.skillAdminAccessDenied
  ) {
    return {
      collapsedLabel: "部门管理",
      collapsedTarget: "/admin/skills",
      collapsible: false,
      items: DEPT_ADMIN_NAVIGATION,
      kind: "dept_admin",
      title: "部门管理",
    };
  }
  return null;
}

/** 底部账户区紧凑角色徽章映射。 */
export function getCompactRoleBadge(role: UserRole): {
  label: string;
  variant: NonNullable<BadgeProps["variant"]>;
} {
  switch (role) {
    case "super_admin":
      return { label: "超管", variant: "brand" };
    case "dept_admin":
      return { label: "部门管", variant: "info" };
    case "member":
      return { label: "员工", variant: "neutral" };
  }
}

export function getUserInitial(name: string): string {
  return Array.from(name.trim())[0]?.toUpperCase() ?? "A";
}

/** 从路由路径安全解析当前活跃会话 ID，排除草稿路由 /chat/new。 */
export function activeSessionIdFromPath(pathname: string): string | null {
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

/** 从路由路径安全解析当前活跃项目 ID。 */
export function activeProjectIdFromPath(pathname: string): string | null {
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

/** 判断带可选 query 参数的侧栏链接是否处于当前激活态。 */
export function isNavTargetActive(
  to: string,
  pathname: string,
  search: string,
): boolean {
  const [targetPath, targetQuery] = to.split("?", 2);
  if (targetQuery) {
    if (pathname !== targetPath && !pathname.startsWith(`${targetPath}/`)) {
      return false;
    }
    const currentParams = new URLSearchParams(search);
    const expectedParams = new URLSearchParams(targetQuery);
    for (const [key, value] of expectedParams.entries()) {
      if (currentParams.get(key) !== value) {
        return false;
      }
    }
    return true;
  }
  return pathname === targetPath || pathname.startsWith(`${targetPath}/`);
}

/** 读取桌面端侧栏折叠偏好；隐私模式或存储受限时默认展开。 */
export function readSidebarCollapsed(): boolean {
  try {
    return (
      typeof window !== "undefined" &&
      window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === "collapsed"
    );
  } catch {
    return false;
  }
}

/** 持久化桌面端侧栏折叠状态到 localStorage。 */
export function writeSidebarCollapsed(collapsed: boolean): void {
  try {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(
        SIDEBAR_COLLAPSED_STORAGE_KEY,
        collapsed ? "collapsed" : "expanded",
      );
    }
  } catch {
    // 浏览器禁用本地存储时仅保持当前会话内存状态，不阻断折叠切换。
  }
}
