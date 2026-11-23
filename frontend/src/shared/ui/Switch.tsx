import * as SwitchPrimitive from "@radix-ui/react-switch";
import * as React from "react";

import { cn } from "./utils";

export type SwitchSize = "sm" | "md";

export interface SwitchProps
  extends React.ComponentPropsWithoutRef<typeof SwitchPrimitive.Root> {
  /** 开关尺寸：sm（表格行内紧凑开关）或 md（表单标准开关）。 */
  size?: SwitchSize;
}

const trackSizeClasses: Record<SwitchSize, string> = {
  sm: "h-5 w-9",
  md: "h-6 w-11",
};

const thumbSizeClasses: Record<SwitchSize, string> = {
  sm: "h-4 w-4 data-[state=checked]:translate-x-4 data-[state=unchecked]:translate-x-0",
  md: "h-5 w-5 data-[state=checked]:translate-x-5 data-[state=unchecked]:translate-x-0",
};

export const Switch = React.forwardRef<
  React.ElementRef<typeof SwitchPrimitive.Root>,
  SwitchProps
>(({ className, size = "md", ...props }, ref) => (
  <SwitchPrimitive.Root
    ref={ref}
    className={cn(
      "peer inline-flex shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent p-0 transition-colors",
      "data-[state=checked]:bg-brand data-[state=unchecked]:bg-border-strong",
      "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
      "disabled:cursor-not-allowed disabled:opacity-50",
      trackSizeClasses[size],
      className,
    )}
    data-compact-control="true"
    data-size={size}
    {...props}
  >
    <SwitchPrimitive.Thumb
      className={cn(
        "pointer-events-none block rounded-full bg-canvas shadow-xs transition-transform",
        thumbSizeClasses[size],
      )}
    />
  </SwitchPrimitive.Root>
));
Switch.displayName = SwitchPrimitive.Root.displayName;
