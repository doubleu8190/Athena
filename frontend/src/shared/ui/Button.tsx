import { Slot, Slottable } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";
import * as React from "react";

import { cn } from "./utils";

export const buttonVariants = cva(
  [
    "relative inline-flex shrink-0 items-center justify-center whitespace-nowrap font-medium",
    "transition-colors duration-150 select-none cursor-pointer",
    "disabled:pointer-events-none disabled:opacity-50",
    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
  ].join(" "),
  {
    variants: {
      variant: {
        primary:
          "border border-transparent bg-brand text-brand-fg shadow-xs hover:bg-brand-hover active:bg-brand",
        secondary:
          "border border-border-default bg-subtle text-text-primary hover:border-border-strong hover:bg-muted",
        outline:
          "border border-border-default bg-canvas text-text-primary hover:border-border-strong hover:bg-subtle",
        ghost:
          "border border-transparent bg-transparent text-text-secondary hover:bg-subtle hover:text-text-primary",
        danger:
          "border border-transparent bg-danger text-white shadow-xs hover:bg-danger/90",
        "danger-outline":
          "border border-danger-border bg-canvas text-danger hover:bg-danger-bg",
      },
      size: {
        xs: "h-7 gap-1.5 rounded-md px-2.5 text-xs",
        sm: "h-8 gap-1.5 rounded-lg px-3 text-xs",
        md: "h-9 gap-2 rounded-lg px-4 text-sm",
        icon: "h-9 w-9 rounded-lg p-0",
      },
    },
    defaultVariants: {
      variant: "primary",
      size: "md",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  /** 将样式与属性透传给唯一子元素（如 React Router Link），避免嵌套交互元素。 */
  asChild?: boolean;
  /** 异步提交中状态：禁用连点、标记 aria-busy，并以居中加载图标替换内容且保持尺寸不抖动。 */
  loading?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      asChild = false,
      children,
      className,
      disabled,
      loading = false,
      size,
      type = "button",
      variant,
      onClick,
      ...props
    },
    ref,
  ) => {
    const isDisabled = Boolean(disabled || loading);
    const isIconSize = size === "icon";

    if (asChild) {
      // asChild 模式下使用 Slottable 保持单个合法 Slot 子节点，并在禁用/加载态阻断交互。
      return (
        <Slot
          ref={ref}
          aria-busy={loading || undefined}
          aria-disabled={isDisabled || undefined}
          className={cn(
            buttonVariants({ variant, size }),
            isDisabled && "pointer-events-none opacity-50",
            className,
          )}
          data-icon-button={isIconSize ? "true" : undefined}
          onClick={
            isDisabled
              ? (event) => {
                  event.preventDefault();
                }
              : onClick
          }
          {...props}
        >
          {loading ? (
            <Loader2
              aria-hidden="true"
              className="h-4 w-4 shrink-0 animate-spin motion-reduce:animate-none"
            />
          ) : null}
          <Slottable>{children}</Slottable>
        </Slot>
      );
    }

    return (
      <button
        ref={ref}
        aria-busy={loading || undefined}
        className={cn(buttonVariants({ variant, size }), className)}
        data-icon-button={isIconSize ? "true" : undefined}
        disabled={isDisabled}
        onClick={onClick}
        type={type}
        {...props}
      >
        {/* 加载态隐藏原内容视觉但保留占位宽度，避免按钮宽度突然坍缩导致布局抖动。 */}
        <span
          className={cn(
            "inline-flex items-center justify-center gap-2",
            loading && "invisible",
          )}
        >
          {children}
        </span>
        {loading ? (
          <Loader2
            aria-hidden="true"
            className="absolute h-4 w-4 animate-spin motion-reduce:animate-none"
          />
        ) : null}
      </button>
    );
  },
);

Button.displayName = "Button";
