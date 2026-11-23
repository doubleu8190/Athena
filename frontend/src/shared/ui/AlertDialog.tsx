import * as AlertDialogPrimitive from "@radix-ui/react-alert-dialog";
import { Slottable } from "@radix-ui/react-slot";
import { Loader2 } from "lucide-react";
import * as React from "react";

import { buttonVariants } from "./Button";
import { cn } from "./utils";

export const AlertDialog = AlertDialogPrimitive.Root;
export const AlertDialogTrigger = AlertDialogPrimitive.Trigger;
export const AlertDialogPortal = AlertDialogPrimitive.Portal;

export const AlertDialogOverlay = React.forwardRef<
  React.ElementRef<typeof AlertDialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof AlertDialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <AlertDialogPrimitive.Overlay
    ref={ref}
    className={cn(
      "fixed inset-0 z-[var(--z-dialog)] bg-black/40 backdrop-blur-[2px]",
      "transition-opacity duration-150 data-[state=closed]:opacity-0 data-[state=open]:opacity-100",
      className,
    )}
    {...props}
  />
));
AlertDialogOverlay.displayName = AlertDialogPrimitive.Overlay.displayName;

export interface AlertDialogContentProps
  extends React.ComponentPropsWithoutRef<typeof AlertDialogPrimitive.Content> {
  /** 提交中状态拦截 Escape 键，防止高危操作进行中被误取消。 */
  closeDisabled?: boolean;
}

export const AlertDialogContent = React.forwardRef<
  React.ElementRef<typeof AlertDialogPrimitive.Content>,
  AlertDialogContentProps
>(({ className, closeDisabled = false, onEscapeKeyDown, ...props }, ref) => (
  <AlertDialogPortal>
    <AlertDialogOverlay />
    <AlertDialogPrimitive.Content
      ref={ref}
      className={cn(
        "fixed top-1/2 left-1/2 z-[var(--z-dialog)] flex w-[calc(100vw-2rem)] max-w-[440px] -translate-x-1/2 -translate-y-1/2 flex-col",
        "rounded-xl border border-border-default bg-canvas p-6 text-text-primary shadow-md",
        "focus-visible:outline-hidden",
        className,
      )}
      onEscapeKeyDown={(event) => {
        if (closeDisabled) {
          event.preventDefault();
          return;
        }
        onEscapeKeyDown?.(event);
      }}
      {...props}
    />
  </AlertDialogPortal>
));
AlertDialogContent.displayName = AlertDialogPrimitive.Content.displayName;

export function AlertDialogHeader({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col gap-2", className)} {...props} />;
}

export const AlertDialogTitle = React.forwardRef<
  React.ElementRef<typeof AlertDialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof AlertDialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <AlertDialogPrimitive.Title
    ref={ref}
    className={cn("text-base font-semibold tracking-tight text-text-primary", className)}
    {...props}
  />
));
AlertDialogTitle.displayName = AlertDialogPrimitive.Title.displayName;

export const AlertDialogDescription = React.forwardRef<
  React.ElementRef<typeof AlertDialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof AlertDialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <AlertDialogPrimitive.Description
    ref={ref}
    className={cn("text-sm leading-relaxed text-text-secondary", className)}
    {...props}
  />
));
AlertDialogDescription.displayName = AlertDialogPrimitive.Description.displayName;

/** 额外确认槽位（如级联删除勾选框或风险提示卡片）。 */
export function AlertDialogBody({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("mt-4", className)} {...props} />;
}

export function AlertDialogFooter({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("mt-6 flex flex-wrap items-center justify-end gap-2", className)}
      {...props}
    />
  );
}

export interface AlertDialogActionProps
  extends React.ComponentPropsWithoutRef<typeof AlertDialogPrimitive.Action> {
  variant?: "primary" | "danger";
  loading?: boolean;
}

export const AlertDialogAction = React.forwardRef<
  React.ElementRef<typeof AlertDialogPrimitive.Action>,
  AlertDialogActionProps
>(
  (
    {
      asChild = false,
      children,
      className,
      disabled,
      loading = false,
      onClick,
      type,
      variant = "primary",
      ...props
    },
    ref,
  ) => {
    const isDisabled = Boolean(disabled || loading);

    if (asChild) {
      // asChild 模式下与 Button 保持一致：使用 Slottable 保持单个合法子元素，并透传 type={type} 覆盖 Radix Close 默认的 type="button"。
      return (
        <AlertDialogPrimitive.Action
          ref={ref}
          aria-busy={loading || undefined}
          aria-disabled={isDisabled || undefined}
          asChild
          className={cn(
            buttonVariants({ variant, size: "md" }),
            isDisabled && "pointer-events-none opacity-50",
            className,
          )}
          onClick={
            isDisabled
              ? (event) => {
                  event.preventDefault();
                }
              : onClick
          }
          type={type}
          {...props}
        >
          {loading ? (
            <Loader2
              aria-hidden="true"
              className="h-4 w-4 shrink-0 animate-spin motion-reduce:animate-none"
            />
          ) : null}
          <Slottable>{children}</Slottable>
        </AlertDialogPrimitive.Action>
      );
    }

    return (
      <AlertDialogPrimitive.Action
        ref={ref}
        aria-busy={loading || undefined}
        className={cn(buttonVariants({ variant, size: "md" }), className)}
        disabled={isDisabled}
        onClick={onClick}
        type={type ?? "button"}
        {...props}
      >
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
      </AlertDialogPrimitive.Action>
    );
  },
);
AlertDialogAction.displayName = AlertDialogPrimitive.Action.displayName;

export const AlertDialogCancel = React.forwardRef<
  React.ElementRef<typeof AlertDialogPrimitive.Cancel>,
  React.ComponentPropsWithoutRef<typeof AlertDialogPrimitive.Cancel>
>(({ asChild = false, className, type, ...props }, ref) => (
  <AlertDialogPrimitive.Cancel
    ref={ref}
    asChild={asChild}
    className={cn(buttonVariants({ variant: "outline", size: "md" }), className)}
    type={asChild ? type : (type ?? "button")}
    {...props}
  />
));
AlertDialogCancel.displayName = AlertDialogPrimitive.Cancel.displayName;
