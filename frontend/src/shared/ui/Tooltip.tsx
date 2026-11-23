import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import * as React from "react";

import { cn } from "./utils";

export const TooltipRoot = TooltipPrimitive.Root;
export const TooltipTrigger = TooltipPrimitive.Trigger;

export type TooltipProviderProps = React.ComponentPropsWithoutRef<
  typeof TooltipPrimitive.Provider
>;

/** 全局 Tooltip 容器，默认悬停 250ms 延迟展示，减少快速划过时的视觉闪烁。 */
export function TooltipProvider({
  delayDuration = 250,
  skipDelayDuration = 150,
  ...props
}: TooltipProviderProps) {
  return (
    <TooltipPrimitive.Provider
      delayDuration={delayDuration}
      skipDelayDuration={skipDelayDuration}
      {...props}
    />
  );
}

export const TooltipContent = React.forwardRef<
  React.ElementRef<typeof TooltipPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>
>(({ className, collisionPadding = 8, sideOffset = 6, ...props }, ref) => (
  <TooltipPrimitive.Portal>
    <TooltipPrimitive.Content
      ref={ref}
      className={cn(
        "z-[var(--z-tooltip)] max-w-xs rounded-md bg-text-primary px-2.5 py-1.5 text-xs font-medium text-brand-fg shadow-sm",
        "select-none",
        className,
      )}
      collisionPadding={collisionPadding}
      sideOffset={sideOffset}
      {...props}
    />
  </TooltipPrimitive.Portal>
));
TooltipContent.displayName = TooltipPrimitive.Content.displayName;

export interface TooltipProps {
  /** 提示文案；为空或 disabled 为 true 时直接渲染子元素，不挂载浮层。 */
  content: React.ReactNode;
  children: React.ReactElement;
  side?: React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>["side"];
  align?: React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>["align"];
  sideOffset?: number;
  delayDuration?: number;
  disabled?: boolean;
  className?: string;
}

/** 便捷 Tooltip 包装组件，适用于图标按钮、截断标题等单触发器提示场景。 */
export function Tooltip({
  align = "center",
  children,
  className,
  content,
  delayDuration,
  disabled = false,
  side = "top",
  sideOffset = 6,
}: TooltipProps) {
  if (
    disabled ||
    content === null ||
    content === undefined ||
    content === "" ||
    typeof content === "boolean"
  ) {
    return children;
  }

  return (
    <TooltipRoot delayDuration={delayDuration}>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent
        align={align}
        className={className}
        side={side}
        sideOffset={sideOffset}
      >
        {content}
      </TooltipContent>
    </TooltipRoot>
  );
}
