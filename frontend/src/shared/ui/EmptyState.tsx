import type { ReactNode } from "react";

import { Icon, type IconName } from "./Icons";
import { cn } from "./utils";

export interface EmptyStateProps {
  action?: ReactNode;
  compact?: boolean;
  description: string;
  icon?: IconName;
  title: string;
  className?: string;
}

/**
 * 全站统一空状态面板：现代清爽中性灰边框与留白风格。
 */
export function EmptyState({
  action,
  className,
  compact = false,
  description,
  icon = "spark",
  title,
}: EmptyStateProps) {
  return (
    <section
      aria-label={title}
      className={cn(
        "flex w-full flex-col items-center justify-center rounded-xl border border-border-subtle bg-canvas text-center",
        compact ? "min-h-[180px] px-5 py-7" : "min-h-[280px] px-6 py-11",
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "mb-3.5 inline-flex items-center justify-center rounded-xl border border-brand-border bg-brand-subtle text-brand",
          compact ? "h-10 w-10" : "h-12 w-12",
        )}
      >
        <Icon name={icon} size={compact ? 18 : 22} />
      </span>

      <div className="flex max-w-md flex-col items-center gap-1.5">
        <h2
          className={cn(
            "font-sans font-semibold tracking-tight text-text-primary",
            compact ? "text-base" : "text-lg",
          )}
        >
          {title}
        </h2>
        <p className="text-sm leading-relaxed text-text-secondary">
          {description}
        </p>
      </div>

      {action ? (
        <div className={cn("flex flex-wrap items-center justify-center gap-2", compact ? "mt-4" : "mt-5")}>
          {action}
        </div>
      ) : null}
    </section>
  );
}
