import * as CheckboxPrimitive from "@radix-ui/react-checkbox";
import { Check, Minus } from "lucide-react";
import * as React from "react";

import { cn } from "./utils";

export type CheckboxProps = React.ComponentPropsWithoutRef<
  typeof CheckboxPrimitive.Root
>;

export const Checkbox = React.forwardRef<
  React.ElementRef<typeof CheckboxPrimitive.Root>,
  CheckboxProps
>(({ checked, className, defaultChecked, onCheckedChange, ...props }, ref) => {
  // 同时追踪非受控态的 checked/indeterminate，确保 defaultChecked="indeterminate" 也能正确显示短横线图标。
  const [uncontrolledState, setUncontrolledState] =
    React.useState<CheckboxPrimitive.CheckedState>(defaultChecked ?? false);
  const currentState = checked !== undefined ? checked : uncontrolledState;

  const handleCheckedChange = React.useCallback(
    (nextState: CheckboxPrimitive.CheckedState) => {
      if (checked === undefined) {
        setUncontrolledState(nextState);
      }
      onCheckedChange?.(nextState);
    },
    [checked, onCheckedChange],
  );

  return (
    <CheckboxPrimitive.Root
      ref={ref}
      checked={checked}
      className={cn(
        "peer inline-flex h-4 w-4 shrink-0 cursor-pointer items-center justify-center rounded border border-border-strong bg-canvas text-brand-fg shadow-xs transition-colors",
        "hover:border-brand",
        "data-[state=checked]:border-brand data-[state=checked]:bg-brand",
        "data-[state=indeterminate]:border-brand data-[state=indeterminate]:bg-brand",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      data-compact-control="true"
      defaultChecked={defaultChecked}
      onCheckedChange={handleCheckedChange}
      {...props}
    >
      <CheckboxPrimitive.Indicator className="flex items-center justify-center text-current">
        {currentState === "indeterminate" ? (
          <Minus aria-hidden="true" className="h-3 w-3 stroke-[2.5]" />
        ) : (
          <Check aria-hidden="true" className="h-3 w-3 stroke-[2.5]" />
        )}
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  );
});
Checkbox.displayName = CheckboxPrimitive.Root.displayName;
