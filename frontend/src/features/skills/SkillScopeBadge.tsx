import { VisibilityBadge, cn } from "../../shared/ui";
import type { SkillScope } from "./model";

interface SkillScopeBadgeProps {
  className?: string;
  compact?: boolean;
  departmentName?: string | null;
  scope: SkillScope;
}

/**
 * 技能三层可见性标签：统一复用全站 VisibilityBadge 原语。
 */
export function SkillScopeBadge({
  className,
  compact = false,
  departmentName,
  scope,
}: SkillScopeBadgeProps) {
  return (
    <VisibilityBadge
      className={cn(
        compact && "px-1.5 py-0 text-[11px] leading-4",
        className,
      )}
      departmentName={departmentName}
      scope={scope}
    />
  );
}
