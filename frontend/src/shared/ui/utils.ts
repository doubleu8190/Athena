import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * 合并条件类名并由 tailwind-merge 消解 Tailwind 工具类冲突，
 * 确保组件外部传入的 className 能够稳定覆盖内置默认样式。
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
