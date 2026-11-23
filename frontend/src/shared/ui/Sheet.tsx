import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import * as React from "react";

import { cn } from "./utils";

export const Sheet = DialogPrimitive.Root;
export const SheetTrigger = DialogPrimitive.Trigger;
export const SheetPortal = DialogPrimitive.Portal;
export const SheetClose = DialogPrimitive.Close;

export const SheetOverlay = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      "fixed inset-0 z-[var(--z-drawer)] bg-black/30",
      "transition-opacity duration-200 data-[state=closed]:opacity-0 data-[state=open]:opacity-100",
      className,
    )}
    {...props}
  />
));
SheetOverlay.displayName = "SheetOverlay";

export type SheetSide = "left" | "right";

const sheetSideClasses: Record<SheetSide, string> = {
  left: "left-0 w-[280px] max-w-[85vw] border-r",
  right: "right-0 w-[380px] max-w-[90vw] border-l",
};

export interface SheetContentProps
  extends React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> {
  /** 侧滑方向：左侧默认 280px（导航抽屉），右侧默认 380px（详情/配置抽屉）。 */
  side?: SheetSide;
  /** 禁用关闭交互（如编辑保存中）。 */
  closeDisabled?: boolean;
  /** 隐藏默认右上角关闭按钮。 */
  hideCloseButton?: boolean;
}

export const SheetContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  SheetContentProps
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
      side = "right",
      ...props
    },
    ref,
  ) => (
    <SheetPortal>
      <SheetOverlay />
      <DialogPrimitive.Content
        ref={ref}
        className={cn(
          "fixed inset-y-0 z-[var(--z-drawer)] flex flex-col border-border-default bg-canvas text-text-primary shadow-md",
          "focus-visible:outline-hidden",
          sheetSideClasses[side],
          className,
        )}
        data-side={side}
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
    </SheetPortal>
  ),
);
SheetContent.displayName = "SheetContent";

export function SheetHeader({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "flex shrink-0 flex-col gap-1 border-b border-border-subtle px-5 py-4 pr-12",
        className,
      )}
      {...props}
    />
  );
}

export const SheetTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title
    ref={ref}
    className={cn("text-base font-semibold tracking-tight text-text-primary", className)}
    {...props}
  />
));
SheetTitle.displayName = "SheetTitle";

export const SheetDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description
    ref={ref}
    className={cn("text-sm text-text-secondary", className)}
    {...props}
  />
));
SheetDescription.displayName = "SheetDescription";

export function SheetBody({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("min-h-0 flex-1 overflow-y-auto px-5 py-4", className)}
      {...props}
    />
  );
}

export function SheetFooter({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-border-subtle px-5 py-4",
        className,
      )}
      {...props}
    />
  );
}
