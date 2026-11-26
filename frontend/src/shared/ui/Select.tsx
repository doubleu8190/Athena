import * as SelectPrimitive from "@radix-ui/react-select";
import { Check, ChevronDown, ChevronUp } from "lucide-react";
import * as React from "react";

import { cn } from "./utils";

export const Select = SelectPrimitive.Root;
export const SelectGroup = SelectPrimitive.Group;
export const SelectValue = SelectPrimitive.Value;

export interface SelectTriggerProps
  extends React.ComponentPropsWithoutRef<typeof SelectPrimitive.Trigger> {
  /** 校验错误态：自动设置 aria-invalid 并切换错误边框色。 */
  error?: boolean;
}

export const SelectTrigger = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Trigger>,
  SelectTriggerProps
>(({ "aria-invalid": ariaInvalid, children, className, error = false, ...props }, ref) => {
  const isInvalid = Boolean(error || ariaInvalid === true || ariaInvalid === "true");

  return (
    <SelectPrimitive.Trigger
      ref={ref}
      aria-invalid={isInvalid || undefined}
      className={cn(
        "flex h-9 w-full items-center justify-between gap-2 rounded-lg border bg-canvas px-3 text-sm text-text-primary shadow-xs transition-colors",
        "data-[placeholder]:text-text-placeholder",
        "focus-visible:outline-hidden focus-visible:ring-2",
        "disabled:cursor-not-allowed disabled:bg-subtle disabled:text-text-tertiary disabled:opacity-70",
        "[&>span]:line-clamp-1",
        isInvalid
          ? "border-danger focus-visible:border-danger focus-visible:ring-danger/20"
          : "border-border-default hover:border-border-strong focus-visible:border-ring-focus focus-visible:ring-ring-focus/20",
        className,
      )}
      {...props}
    >
      {children}
      <SelectPrimitive.Icon asChild>
        <ChevronDown
          aria-hidden="true"
          className="h-4 w-4 shrink-0 text-text-tertiary"
        />
      </SelectPrimitive.Icon>
    </SelectPrimitive.Trigger>
  );
});
SelectTrigger.displayName = SelectPrimitive.Trigger.displayName;

export const SelectScrollUpButton = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.ScrollUpButton>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.ScrollUpButton>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.ScrollUpButton
    ref={ref}
    className={cn(
      "flex cursor-default items-center justify-center py-1 text-text-tertiary",
      className,
    )}
    {...props}
  >
    <ChevronUp aria-hidden="true" className="h-4 w-4" />
  </SelectPrimitive.ScrollUpButton>
));
SelectScrollUpButton.displayName = SelectPrimitive.ScrollUpButton.displayName;

export const SelectScrollDownButton = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.ScrollDownButton>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.ScrollDownButton>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.ScrollDownButton
    ref={ref}
    className={cn(
      "flex cursor-default items-center justify-center py-1 text-text-tertiary",
      className,
    )}
    {...props}
  >
    <ChevronDown aria-hidden="true" className="h-4 w-4" />
  </SelectPrimitive.ScrollDownButton>
));
SelectScrollDownButton.displayName = SelectPrimitive.ScrollDownButton.displayName;

export const SelectContent = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Content>
>(
  (
    {
      children,
      className,
      collisionPadding = 8,
      position = "popper",
      sideOffset = 4,
      ...props
    },
    ref,
  ) => (
    <SelectPrimitive.Portal>
      <SelectPrimitive.Content
        ref={ref}
        // 声明由下拉浮层自身优先消费 Escape，避免嵌套在旧 Modal 内时被 capture 阶段监听抢先关闭弹窗。
        data-modal-escape-owner="self"
        className={cn(
          "relative z-[var(--z-popover)] max-h-72 min-w-[8rem] overflow-hidden rounded-lg border border-border-default bg-canvas text-text-primary shadow-md",
          position === "popper" &&
            "w-[var(--radix-select-trigger-width)] min-w-[var(--radix-select-trigger-width)]",
          className,
        )}
        collisionPadding={collisionPadding}
        position={position}
        sideOffset={sideOffset}
        {...props}
      >
        <SelectScrollUpButton />
        <SelectPrimitive.Viewport className="p-1">
          {children}
        </SelectPrimitive.Viewport>
        <SelectScrollDownButton />
      </SelectPrimitive.Content>
    </SelectPrimitive.Portal>
  ),
);
SelectContent.displayName = SelectPrimitive.Content.displayName;

export const SelectLabel = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Label>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Label>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.Label
    ref={ref}
    className={cn("px-2.5 py-1.5 text-xs font-medium text-text-tertiary", className)}
    {...props}
  />
));
SelectLabel.displayName = SelectPrimitive.Label.displayName;

export const SelectItem = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Item>
>(({ children, className, ...props }, ref) => (
  <SelectPrimitive.Item
    ref={ref}
    className={cn(
      "relative flex w-full cursor-pointer items-center rounded-md py-1.5 pr-8 pl-2.5 text-sm text-text-primary outline-hidden select-none transition-colors",
      "focus:bg-subtle data-[state=checked]:font-medium data-[state=checked]:text-brand",
      "data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
      className,
    )}
    {...props}
  >
    <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
    <span className="absolute right-2.5 inline-flex h-4 w-4 items-center justify-center">
      <SelectPrimitive.ItemIndicator>
        <Check aria-hidden="true" className="h-3.5 w-3.5 text-brand" />
      </SelectPrimitive.ItemIndicator>
    </span>
  </SelectPrimitive.Item>
));
SelectItem.displayName = SelectPrimitive.Item.displayName;

export const SelectSeparator = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Separator>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Separator>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.Separator
    ref={ref}
    className={cn("-mx-1 my-1 h-px bg-border-subtle", className)}
    {...props}
  />
));
SelectSeparator.displayName = SelectPrimitive.Separator.displayName;
