import * as React from "react";

import { cn } from "./utils";

export type SkeletonProps = React.HTMLAttributes<HTMLDivElement>;

/** 轻量中性灰骨架占位块，在 prefers-reduced-motion 下自动停用脉冲动画。 */
export function Skeleton({ className, ...props }: SkeletonProps) {
  return (
    <div
      aria-hidden="true"
      className={cn(
        "animate-pulse rounded-md bg-muted motion-reduce:animate-none",
        className,
      )}
      {...props}
    />
  );
}
