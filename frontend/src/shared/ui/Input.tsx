import { Eye, EyeOff } from "lucide-react";
import * as React from "react";

import { cn } from "./utils";

export interface InputProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "prefix"> {
  /** 校验错误态：自动设置 aria-invalid 并切换错误边框色。 */
  error?: boolean;
  /** 输入框内部左侧前置槽位（如搜索图标或协议前缀）。 */
  prefixSlot?: React.ReactNode;
  /** 输入框内部右侧后置槽位（如单位或清空操作）。 */
  suffixSlot?: React.ReactNode;
  /** 启用密码显隐切换按钮，使用标准 button + aria-pressed 实现无障碍切换。 */
  passwordToggle?: boolean;
  /** 内部原生 input 元素的额外类名。 */
  inputClassName?: string;
}

export const Input = React.forwardRef<HTMLInputElement, InputProps>(
  (
    {
      "aria-invalid": ariaInvalid,
      className,
      disabled,
      error = false,
      inputClassName,
      passwordToggle = false,
      prefixSlot,
      suffixSlot,
      type = "text",
      ...props
    },
    ref,
  ) => {
    const [passwordVisible, setPasswordVisible] = React.useState(false);
    const isInvalid = Boolean(error || ariaInvalid === true || ariaInvalid === "true");

    // 启用密码切换时，按显隐状态在 text 与 password 间切换输入类型。
    const resolvedType = passwordToggle
      ? passwordVisible
        ? "text"
        : "password"
      : type;

    const hasAdornment = Boolean(prefixSlot || suffixSlot || passwordToggle);

    if (!hasAdornment) {
      return (
        <input
          ref={ref}
          aria-invalid={isInvalid || undefined}
          className={cn(
            "flex h-9 w-full rounded-lg border bg-canvas px-3 text-sm text-text-primary shadow-xs transition-colors",
            "placeholder:text-text-placeholder",
            "focus-visible:outline-hidden focus-visible:ring-2",
            "disabled:cursor-not-allowed disabled:bg-subtle disabled:text-text-tertiary disabled:opacity-70",
            isInvalid
              ? "border-danger focus-visible:border-danger focus-visible:ring-danger/20"
              : "border-border-default hover:border-border-strong focus-visible:border-ring-focus focus-visible:ring-ring-focus/20",
            className,
            inputClassName,
          )}
          disabled={disabled}
          type={resolvedType}
          {...props}
        />
      );
    }

    return (
      <div
        className={cn(
          "flex h-9 w-full items-center gap-2 rounded-lg border bg-canvas px-3 shadow-xs transition-colors",
          "focus-within:ring-2 forced-colors:focus-within:outline-2 forced-colors:focus-within:outline-[Highlight]",
          disabled && "cursor-not-allowed bg-subtle opacity-70",
          isInvalid
            ? "border-danger focus-within:border-danger focus-within:ring-danger/20"
            : "border-border-default hover:border-border-strong focus-within:border-ring-focus focus-within:ring-ring-focus/20",
          className,
        )}
      >
        {prefixSlot ? (
          <span className="inline-flex shrink-0 items-center text-text-tertiary">
            {prefixSlot}
          </span>
        ) : null}

        <input
          ref={ref}
          aria-invalid={isInvalid || undefined}
          className={cn(
            "h-full min-w-0 flex-1 border-0 bg-transparent text-sm text-text-primary",
            "placeholder:text-text-placeholder focus-visible:outline-hidden",
            "disabled:cursor-not-allowed disabled:text-text-tertiary",
            inputClassName,
          )}
          disabled={disabled}
          type={resolvedType}
          {...props}
        />

        {suffixSlot ? (
          <span className="inline-flex shrink-0 items-center text-text-tertiary">
            {suffixSlot}
          </span>
        ) : null}

        {passwordToggle ? (
          <button
            aria-label={passwordVisible ? "隐藏密码" : "显示密码"}
            aria-pressed={passwordVisible}
            className={cn(
              "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-text-tertiary",
              "transition-colors hover:bg-subtle hover:text-text-primary",
              "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring-focus",
              "disabled:pointer-events-none disabled:opacity-50",
            )}
            data-compact-control="true"
            disabled={disabled}
            onClick={() => setPasswordVisible((prev) => !prev)}
            type="button"
          >
            {passwordVisible ? (
              <EyeOff aria-hidden="true" className="h-4 w-4" />
            ) : (
              <Eye aria-hidden="true" className="h-4 w-4" />
            )}
          </button>
        ) : null}
      </div>
    );
  },
);

Input.displayName = "Input";
