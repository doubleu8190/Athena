import { VisibilityBadge, cn } from "../../shared/ui";
import {
  skillScope,
  type LegacyCompatibleSkillScope,
} from "./model";

interface SkillScopeBadgeProps {
  className?: string;
  compact?: boolean;
  departmentName?: string | null;
  scope: LegacyCompatibleSkillScope | undefined;
}

/**
 * 技能三层可见性标签：统一复用全站 VisibilityBadge 原语，自动兼容历史 "public" 枚举。
 */
export function SkillScopeBadge({
  className,
  compact = false,
  departmentName,
  scope,
}: SkillScopeBadgeProps) {
  const normalizedScope = skillScope({ scope });

  return (
    <VisibilityBadge
      className={cn(
        compact && "px-1.5 py-0 text-[11px] leading-4",
        className,
      )}
      departmentName={departmentName}
      scope={normalizedScope}
    />
  );
}
