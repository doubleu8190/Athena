import type { SkillScope } from "./model";

interface SkillScopeBadgeProps {
  compact?: boolean;
  scope: SkillScope | undefined;
}

export function SkillScopeBadge({
  compact = false,
  scope,
}: SkillScopeBadgeProps) {
  const normalizedScope: SkillScope =
    scope === "personal" ? "personal" : "public";
  const label = normalizedScope === "personal" ? "个人" : "公共";

  return (
    <span
      className={`skill-scope-badge is-${normalizedScope}${
        compact ? " is-compact" : ""
      }`}
    >
      {label}
    </span>
  );
}
