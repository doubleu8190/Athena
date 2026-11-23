import { cn } from "./utils";

export interface BrandProps {
  /** 紧凑模式：隐藏副标题，仅展示图形标志与主字标。 */
  compact?: boolean;
  /** 深色背景适配模式（如登录页品牌侧栏）。 */
  light?: boolean;
  /** 仅渲染品牌几何图标（用于窄边栏或极简入口）。 */
  markOnly?: boolean;
  className?: string;
}

/**
 * Athena v2 品牌标识：深绿几何标志搭配现代无衬线字标，保持既有 Props 完全向后兼容。
 */
export function Brand({
  className,
  compact = false,
  light = false,
  markOnly = false,
}: BrandProps) {
  return (
    <span
      className={cn(
        "inline-flex min-w-0 items-center gap-2.5 select-none",
        light ? "text-white" : "text-text-primary",
        className,
      )}
      data-brand-light={light ? "true" : undefined}
      data-mark-only={markOnly ? "true" : undefined}
    >
      <span
        aria-hidden="true"
        className={cn(
          "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border shadow-xs",
          light
            ? "border-white/20 bg-white/10 text-white"
            : "border-brand/15 bg-brand text-brand-fg",
        )}
      >
        <svg
          aria-hidden="true"
          className="h-4 w-4"
          fill="none"
          viewBox="0 0 24 24"
        >
          {/* 几何化 A 字折面与中轴星点，传达沉稳、清晰的企业智能工作台气质。 */}
          <path
            d="M12 3.5L20 19.5H16.2L14.3 15.6H9.7L7.8 19.5H4L12 3.5Z"
            fill="currentColor"
          />
          <circle
            cx="12"
            cy="12.5"
            fill={light ? "#16362c" : "#ebf4f0"}
            r="1.9"
          />
        </svg>
      </span>

      {!markOnly ? (
        <span className="flex min-w-0 flex-col">
          <span className="font-sans text-[17px] leading-tight font-semibold tracking-tight">
            Athena
          </span>
          {!compact ? (
            <span
              className={cn(
                "truncate font-sans text-[11px] leading-tight font-medium",
                light ? "text-white/75" : "text-text-tertiary",
              )}
            >
              企业智能工作台
            </span>
          ) : null}
        </span>
      ) : null}
    </span>
  );
}
