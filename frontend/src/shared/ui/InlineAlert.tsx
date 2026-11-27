import { cva, type VariantProps } from "class-variance-authority";
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  Info,
} from "lucide-react";
import * as React from "react";

import { cn } from "./utils";

const inlineAlertVariants = cva("flex items-start border", {
  variants: {
    size: {
      default: "gap-3 rounded-lg px-3.5 py-3 text-sm leading-5",
      compact:
        "gap-[9px] rounded-[9px] px-3 py-[11px] text-xs leading-relaxed",
    },
    variant: {
      info: "border-info-border bg-info-bg text-info",
      warning: "border-warning-border bg-warning-bg text-warning",
      danger: "border-danger-border bg-danger-bg text-danger",
      success: "border-success-border bg-success-bg text-success",
    },
  },
  defaultVariants: {
    size: "default",
    variant: "info",
  },
});

export type InlineAlertVariant = NonNullable<
  VariantProps<typeof inlineAlertVariants>["variant"]
>;
export type InlineAlertSize = NonNullable<
  VariantProps<typeof inlineAlertVariants>["size"]
>;

export interface InlineAlertProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  /** 语义变体：danger/warning 自动使用 role="alert"，info/success 使用 role="status"。 */
  variant?: InlineAlertVariant;
  /** 尺寸预设：compact 用于页面/弹窗内嵌紧凑错误条，替代原 ErrorBanner 壳组件。 */
  size?: InlineAlertSize;
  /** 提示标题。 */
  title?: React.ReactNode;
  /** 提示详细描述。 */
  description?: React.ReactNode;
  /** 右侧可选操作槽位（例如“重试”按钮）。 */
  action?: React.ReactNode;
}

const variantIconMap: Record<
  InlineAlertVariant,
  React.ComponentType<{ className?: string; "aria-hidden"?: boolean | "true" | "false" }>
> = {
  info: Info,
  warning: AlertTriangle,
  danger: AlertCircle,
  success: CheckCircle2,
};

export function InlineAlert({
  action,
  children,
  className,
  description,
  role,
  size = "default",
  title,
  variant = "info",
  ...props
}: InlineAlertProps) {
  const IconComponent = variantIconMap[variant];
  // 按无障碍语义：错误与告警需即时播报（alert），普通信息与成功反馈使用温和状态区（status）。
  const resolvedRole =
    role ?? (variant === "danger" || variant === "warning" ? "alert" : "status");

  return (
    <div
      className={cn(inlineAlertVariants({ size, variant }), className)}
      data-variant={variant}
      role={resolvedRole}
      {...props}
    >
      <IconComponent
        aria-hidden="true"
        className="mt-0.5 h-4 w-4 shrink-0"
      />
      <div className="min-w-0 flex-1 space-y-0.5">
        {title ? <div className="font-medium">{title}</div> : null}
        {description ? (
          <div className="text-xs leading-relaxed opacity-90">{description}</div>
        ) : null}
        {children}
      </div>
      {action ? <div className="shrink-0 self-center">{action}</div> : null}
    </div>
  );
}
