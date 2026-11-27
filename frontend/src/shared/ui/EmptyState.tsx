import {
  BookText,
  Folder,
  History,
  MessageCircle,
  Search,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Sparkle,
  Upload,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "./utils";

// EmptyState 仅内部维护空状态常用图标映射，避免依赖已淘汰的全局 Icons 壳组件。
const EMPTY_STATE_ICONS = {
  book: BookText,
  chat: MessageCircle,
  folder: Folder,
  history: History,
  search: Search,
  settings: Settings,
  shield: ShieldCheck,
  sliders: SlidersHorizontal,
  spark: Sparkle,
  tools: Wrench,
  upload: Upload,
} as const satisfies Record<string, LucideIcon>;

export type EmptyStateIconName = keyof typeof EMPTY_STATE_ICONS;

export interface EmptyStateProps {
  action?: ReactNode;
  compact?: boolean;
  description: string;
  icon?: EmptyStateIconName;
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
  const IconComponent = EMPTY_STATE_ICONS[icon];
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
        <IconComponent
          aria-hidden="true"
          focusable="false"
          size={compact ? 18 : 22}
          strokeWidth={1.7}
        />
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
