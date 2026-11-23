import * as PopoverPrimitive from "@radix-ui/react-popover";
import * as React from "react";

import { cn } from "./utils";

export const Popover = PopoverPrimitive.Root;
export const PopoverTrigger = PopoverPrimitive.Trigger;
export const PopoverAnchor = PopoverPrimitive.Anchor;
export const PopoverClose = PopoverPrimitive.Close;

export const PopoverContent = React.forwardRef<
  React.ElementRef<typeof PopoverPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof PopoverPrimitive.Content>
>(
  (
    {
      align = "center",
      className,
      collisionPadding = 8,
      sideOffset = 6,
      ...props
    },
    ref,
  ) => (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Content
        ref={ref}
        align={align}
        className={cn(
          "z-[var(--z-popover)] w-72 rounded-xl border border-border-default bg-canvas p-4 text-text-primary shadow-md",
          "outline-hidden focus-visible:outline-hidden",
          className,
        )}
        collisionPadding={collisionPadding}
        sideOffset={sideOffset}
        {...props}
      />
    </PopoverPrimitive.Portal>
  ),
);
PopoverContent.displayName = PopoverPrimitive.Content.displayName;
