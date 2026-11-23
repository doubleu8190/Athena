import { Building2, User, Users } from "lucide-react";
import * as React from "react";

import { cn } from "./utils";

export type VisibilityScope = "company" | "department" | "personal";

export interface VisibilityBadgeProps
  extends React.HTMLAttributes<HTMLSpanElement> {
  /** 三层可见性范围：全公司 / 部门共享 / 个人。 */
  scope: VisibilityScope;
  /** 当 scope 为 department 时可选展示具体部门名称；为空时回退展示“部门共享”。 */
  departmentName?: string | null;
}

const scopeStyles: Record<VisibilityScope, string> = {
  company:
    "border-scope-company-border bg-scope-company-bg text-scope-company-fg",
  department:
    "border-scope-department-border bg-scope-department-bg text-scope-department-fg",
  personal:
    "border-scope-personal-border bg-scope-personal-bg text-scope-personal-fg",
};

/**
 * 统一全站三层可见性标签（知识库、技能目录、项目管理共享同一套语义图标与色彩）。
 */
export function VisibilityBadge({
  className,
  departmentName,
  scope,
  ...props
}: VisibilityBadgeProps) {
  const trimmedDepartment = departmentName?.trim();
  const label =
    scope === "company"
      ? "全公司"
      : scope === "department"
        ? trimmedDepartment || "部门共享"
        : "个人";

  const IconComponent =
    scope === "company" ? Building2 : scope === "department" ? Users : User;

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium leading-4 whitespace-nowrap",
        scopeStyles[scope],
        className,
      )}
      data-scope={scope}
      {...props}
    >
      <IconComponent aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
      <span>{label}</span>
    </span>
  );
}
