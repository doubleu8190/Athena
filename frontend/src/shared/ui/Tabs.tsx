import * as TabsPrimitive from "@radix-ui/react-tabs";
import * as React from "react";

import { cn } from "./utils";

export type TabsVariant = "underline" | "segmented";

const TabsVariantContext = React.createContext<TabsVariant>("underline");

export interface TabsProps
  extends React.ComponentPropsWithoutRef<typeof TabsPrimitive.Root> {
  /** 标签页视觉变体：underline（页面/工作区主切换）或 segmented（紧凑筛选/开关分组）。 */
  variant?: TabsVariant;
}

export const Tabs = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Root>,
  TabsProps
>(({ variant = "underline", ...props }, ref) => (
  <TabsVariantContext.Provider value={variant}>
    <TabsPrimitive.Root ref={ref} data-variant={variant} {...props} />
  </TabsVariantContext.Provider>
));
Tabs.displayName = TabsPrimitive.Root.displayName;

export interface TabsListProps
  extends React.ComponentPropsWithoutRef<typeof TabsPrimitive.List> {
  variant?: TabsVariant;
}

export const TabsList = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.List>,
  TabsListProps
>(({ className, variant, ...props }, ref) => {
  const inheritedVariant = React.useContext(TabsVariantContext);
  const resolvedVariant = variant ?? inheritedVariant;

  return (
    <TabsVariantContext.Provider value={resolvedVariant}>
      <TabsPrimitive.List
        ref={ref}
        className={cn(
          resolvedVariant === "segmented"
            ? "inline-flex items-center gap-1 rounded-lg border border-border-subtle bg-subtle p-1"
            : "inline-flex items-center gap-5 border-b border-border-subtle",
          className,
        )}
        data-variant={resolvedVariant}
        {...props}
      />
    </TabsVariantContext.Provider>
  );
});
TabsList.displayName = TabsPrimitive.List.displayName;

export interface TabsTriggerProps
  extends React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger> {
  variant?: TabsVariant;
}

export const TabsTrigger = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Trigger>,
  TabsTriggerProps
>(({ className, variant, ...props }, ref) => {
  const inheritedVariant = React.useContext(TabsVariantContext);
  const resolvedVariant = variant ?? inheritedVariant;

  return (
    <TabsPrimitive.Trigger
      ref={ref}
      className={cn(
        "inline-flex cursor-pointer items-center justify-center gap-1.5 text-sm font-medium whitespace-nowrap transition-colors select-none",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
        "disabled:pointer-events-none disabled:opacity-50",
        resolvedVariant === "segmented"
          ? [
              "rounded-md px-3 py-1.5 text-text-secondary hover:text-text-primary",
              "data-[state=active]:bg-canvas data-[state=active]:text-text-primary data-[state=active]:shadow-xs",
            ].join(" ")
          : [
              "-mb-px border-b-2 border-transparent px-1 py-2.5 text-text-secondary hover:text-text-primary",
              "data-[state=active]:border-brand data-[state=active]:text-brand",
            ].join(" "),
        className,
      )}
      {...props}
    />
  );
});
TabsTrigger.displayName = TabsPrimitive.Trigger.displayName;

export const TabsContent = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Content
    ref={ref}
    className={cn(
      "mt-4 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
      className,
    )}
    {...props}
  />
));
TabsContent.displayName = TabsPrimitive.Content.displayName;
