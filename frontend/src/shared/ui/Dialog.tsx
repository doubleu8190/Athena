import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import * as React from "react";

import { cn } from "./utils";

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogPortal = DialogPrimitive.Portal;
export const DialogClose = DialogPrimitive.Close;

export const DialogOverlay = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      "fixed inset-0 z-[var(--z-dialog)] bg-black/35 backdrop-blur-[2px]",
      "transition-opacity duration-150 data-[state=closed]:opacity-0 data-[state=open]:opacity-100",
      className,
    )}
    {...props}
  />
));
DialogOverlay.displayName = DialogPrimitive.Overlay.displayName;

export type DialogSize = "sm" | "md" | "lg";

const dialogSizeClasses: Record<DialogSize, string> = {
  sm: "max-w-[420px]",
  md: "max-w-[540px]",
  lg: "max-w-[680px]",
};

export interface DialogContentProps
  extends React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> {
  /** 弹窗宽度档位：sm(420px) / md(540px) / lg(680px)。 */
  size?: DialogSize;
  /** 禁用关闭交互（提交中场景）：拦截 Escape、外部点击并禁用右上角关闭按钮。 */
  closeDisabled?: boolean;
  /** 隐藏默认右上角关闭按钮。 */
  hideCloseButton?: boolean;
}

export const DialogContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  DialogContentProps
>(
  (
    {
      children,
      className,
      closeDisabled = false,
      hideCloseButton = false,
      onEscapeKeyDown,
      onInteractOutside,
      onPointerDownOutside,
      size = "md",
      ...props
    },
    ref,
  ) => (
    <DialogPortal>
      <DialogOverlay />
      <DialogPrimitive.Content
        ref={ref}
        className={cn(
          "fixed top-1/2 left-1/2 z-[var(--z-dialog)] flex max-h-[calc(100vh-2rem)] w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col",
          "rounded-xl border border-border-default bg-canvas text-text-primary shadow-md",
          "focus-visible:outline-hidden",
          dialogSizeClasses[size],
          className,
        )}
        onEscapeKeyDown={(event) => {
          if (closeDisabled) {
            event.preventDefault();
            return;
          }
          onEscapeKeyDown?.(event);
        }}
        onInteractOutside={(event) => {
          if (closeDisabled) {
            event.preventDefault();
            return;
          }
          onInteractOutside?.(event);
        }}
        onPointerDownOutside={(event) => {
          if (closeDisabled) {
            event.preventDefault();
            return;
          }
          onPointerDownOutside?.(event);
        }}
        {...props}
      >
        {children}
        {!hideCloseButton ? (
          <DialogPrimitive.Close
            aria-label="关闭"
            className={cn(
              "absolute top-4 right-4 inline-flex h-8 w-8 items-center justify-center rounded-lg text-text-tertiary",
              "transition-colors hover:bg-subtle hover:text-text-primary",
              "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
              "disabled:pointer-events-none disabled:opacity-40",
            )}
            data-compact-control="true"
            disabled={closeDisabled}
            type="button"
          >
            <X aria-hidden="true" className="h-4 w-4" />
          </DialogPrimitive.Close>
        ) : null}
      </DialogPrimitive.Content>
    </DialogPortal>
  ),
);
DialogContent.displayName = DialogPrimitive.Content.displayName;

export function DialogHeader({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "flex shrink-0 flex-col gap-1.5 border-b border-border-subtle px-6 py-4 pr-12",
        className,
      )}
      {...props}
    />
  );
}

export const DialogTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title
    ref={ref}
    className={cn("text-base font-semibold tracking-tight text-text-primary", className)}
    {...props}
  />
));
DialogTitle.displayName = DialogPrimitive.Title.displayName;

export const DialogDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description
    ref={ref}
    className={cn("text-sm text-text-secondary", className)}
    {...props}
  />
));
DialogDescription.displayName = DialogPrimitive.Description.displayName;

export function DialogBody({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("min-h-0 flex-1 overflow-y-auto px-6 py-4", className)}
      {...props}
    />
  );
}

export function DialogFooter({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-border-subtle px-6 py-4",
        className,
      )}
      {...props}
    />
  );
}
