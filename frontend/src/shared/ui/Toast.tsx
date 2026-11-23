import type * as React from "react";
import { Toaster as SonnerToaster, toast, type ToasterProps } from "sonner";

import { cn } from "./utils";

export { toast };

type SonnerTokenStyle = React.CSSProperties &
  Record<string, string | number | undefined>;

/**
 * 全站轻提示容器：定位右下角，通过 Sonner 原生 CSS 变量与 richColors 对齐 Design Tokens，
 * 确保默认中性通知与四种语义状态均不被 Sonner 未分层默认样式覆盖。
 */
export function Toaster({
  className,
  position = "bottom-right",
  richColors = true,
  style,
  theme = "light",
  toastOptions,
  ...props
}: ToasterProps) {
  const tokenStyle: SonnerTokenStyle = {
    "--border-radius": "0.75rem",
    "--normal-bg": "var(--bg-canvas)",
    "--normal-border": "var(--border-default)",
    "--normal-text": "var(--text-primary)",
    "--normal-bg-hover": "var(--bg-subtle)",
    "--normal-border-hover": "var(--border-strong)",
    // 通过 --toast-* 非同名别名引用语义色，避免同元素上的 --success-bg: var(--success-bg) 循环依赖失效。
    "--success-bg": "var(--toast-success-bg)",
    "--success-border": "var(--toast-success-border)",
    "--success-text": "var(--success-fg)",
    "--error-bg": "var(--danger-bg)",
    "--error-border": "var(--danger-border)",
    "--error-text": "var(--danger-fg)",
    "--warning-bg": "var(--toast-warning-bg)",
    "--warning-border": "var(--toast-warning-border)",
    "--warning-text": "var(--warning-fg)",
    "--info-bg": "var(--toast-info-bg)",
    "--info-border": "var(--toast-info-border)",
    "--info-text": "var(--info-fg)",
    zIndex: "var(--z-toast)",
    ...style,
  };

  return (
    <SonnerToaster
      className={cn("font-sans", className)}
      position={position}
      richColors={richColors}
      style={tokenStyle}
      theme={theme}
      toastOptions={{
        ...toastOptions,
        classNames: {
          toast: cn(
            "group !rounded-xl !shadow-md",
            toastOptions?.classNames?.toast,
          ),
          title: cn(
            "!text-sm !font-medium",
            toastOptions?.classNames?.title,
          ),
          description: cn(
            "!text-xs !opacity-90",
            toastOptions?.classNames?.description,
          ),
          actionButton: cn(
            "!rounded-md !bg-brand !text-brand-fg !text-xs !font-medium",
            toastOptions?.classNames?.actionButton,
          ),
          cancelButton: cn(
            "!rounded-md !border !border-border-default !bg-subtle !text-text-secondary !text-xs !font-medium",
            toastOptions?.classNames?.cancelButton,
          ),
          closeButton: cn(
            "!border-border-default !bg-canvas !text-text-tertiary hover:!text-text-primary",
            toastOptions?.classNames?.closeButton,
          ),
        },
      }}
      {...props}
    />
  );
}
