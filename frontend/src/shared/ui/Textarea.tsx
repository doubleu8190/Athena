import * as React from "react";

import { cn } from "./utils";

function resolveLength(value: string | number | readonly string[] | undefined): number {
  if (typeof value === "string") {
    return value.length;
  }
  if (typeof value === "number") {
    return String(value).length;
  }
  if (Array.isArray(value)) {
    return value.join("").length;
  }
  return 0;
}

export interface TextareaProps
  extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  /** 校验错误态：自动设置 aria-invalid 并切换错误边框色。 */
  error?: boolean;
  /** 是否显示实时字数统计；传入 maxLength 时默认展示，可设为 false 显式隐藏。 */
  showCount?: boolean;
  /** 外层包裹容器类名（仅启用字数统计槽位时生效）。 */
  wrapperClassName?: string;
}

export const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  (
    {
      "aria-invalid": ariaInvalid,
      className,
      defaultValue,
      error = false,
      maxLength,
      onChange,
      showCount,
      value,
      wrapperClassName,
      ...props
    },
    ref,
  ) => {
    const isControlled = value !== undefined;
    const [uncontrolledLength, setUncontrolledLength] = React.useState(() =>
      resolveLength(defaultValue),
    );
    const currentLength = isControlled ? resolveLength(value) : uncontrolledLength;
    const shouldShowCount = showCount ?? maxLength !== undefined;
    const isInvalid = Boolean(error || ariaInvalid === true || ariaInvalid === "true");
    const isAtLimit = maxLength !== undefined && currentLength >= maxLength;

    const handleChange = React.useCallback(
      (event: React.ChangeEvent<HTMLTextAreaElement>) => {
        if (!isControlled) {
          setUncontrolledLength(event.target.value.length);
        }
        onChange?.(event);
      },
      [isControlled, onChange],
    );

    const textareaElement = (
      <textarea
        ref={ref}
        aria-invalid={isInvalid || undefined}
        className={cn(
          "flex min-h-[84px] w-full rounded-lg border bg-canvas px-3 py-2 text-sm leading-relaxed text-text-primary shadow-xs transition-colors",
          "placeholder:text-text-placeholder",
          "focus-visible:outline-hidden focus-visible:ring-2",
          "disabled:cursor-not-allowed disabled:bg-subtle disabled:text-text-tertiary disabled:opacity-70",
          isInvalid
            ? "border-danger focus-visible:border-danger focus-visible:ring-danger/20"
            : "border-border-default hover:border-border-strong focus-visible:border-ring-focus focus-visible:ring-ring-focus/20",
          className,
        )}
        defaultValue={defaultValue}
        maxLength={maxLength}
        onChange={handleChange}
        value={value}
        {...props}
      />
    );

    if (!shouldShowCount) {
      return textareaElement;
    }

    return (
      <div className={cn("flex w-full flex-col gap-1.5", wrapperClassName)}>
        {textareaElement}
        <div
          aria-live="polite"
          className={cn(
            "self-end font-mono text-xs tabular-nums",
            isAtLimit ? "text-warning" : "text-text-tertiary",
          )}
        >
          {maxLength !== undefined ? `${currentLength} / ${maxLength}` : currentLength}
        </div>
      </div>
    );
  },
);

Textarea.displayName = "Textarea";

export interface AutoResizeTextareaProps extends TextareaProps {
  /** 最小显示行数，决定初始最小高度。 */
  minRows?: number;
  /** 最大自增高行数，超出后转为内部纵向滚动而不截断内容。 */
  maxRows?: number;
  /** 显式指定最大像素高度（优先级高于 maxRows）。 */
  maxHeight?: number;
}

const DEFAULT_LINE_HEIGHT_PX = 22;
const DEFAULT_VERTICAL_PADDING_PX = 18;

export const AutoResizeTextarea = React.forwardRef<
  HTMLTextAreaElement,
  AutoResizeTextareaProps
>(
  (
    {
      className,
      maxHeight,
      maxRows = 8,
      minRows = 3,
      onChange,
      onInput,
      rows,
      style,
      value,
      ...props
    },
    forwardedRef,
  ) => {
    const innerRef = React.useRef<HTMLTextAreaElement | null>(null);

    const setRefs = React.useCallback(
      (node: HTMLTextAreaElement | null) => {
        innerRef.current = node;
        if (typeof forwardedRef === "function") {
          forwardedRef(node);
        } else if (forwardedRef) {
          forwardedRef.current = node;
        }
      },
      [forwardedRef],
    );

    const resizeToContent = React.useCallback(() => {
      const element = innerRef.current;
      if (!element) {
        return;
      }

      const computedStyle = window.getComputedStyle(element);
      const parsedLineHeight = Number.parseFloat(computedStyle.lineHeight);
      const lineHeight = Number.isFinite(parsedLineHeight)
        ? parsedLineHeight
        : DEFAULT_LINE_HEIGHT_PX;
      const paddingY =
        (Number.parseFloat(computedStyle.paddingTop) || 0) +
        (Number.parseFloat(computedStyle.paddingBottom) || 0);
      const borderY =
        (Number.parseFloat(computedStyle.borderTopWidth) || 0) +
        (Number.parseFloat(computedStyle.borderBottomWidth) || 0);
      const chromeHeight = paddingY + borderY || DEFAULT_VERTICAL_PADDING_PX;

      const minPx = minRows * lineHeight + chromeHeight;
      const maxPx = maxHeight ?? maxRows * lineHeight + chromeHeight;
      const effectiveMinPx = Math.min(minPx, maxPx);

      // border-box 模式下 scrollHeight 不含上下边框，需补回 borderY 再按上下界收敛并保留必要滚动。
      element.style.height = "auto";
      const measuredHeight = element.scrollHeight + borderY;
      const nextHeight = Math.max(
        effectiveMinPx,
        Math.min(measuredHeight, maxPx),
      );
      element.style.height = `${nextHeight}px`;
      element.style.overflowY = measuredHeight > maxPx ? "auto" : "hidden";
    }, [maxHeight, maxRows, minRows]);

    React.useLayoutEffect(() => {
      resizeToContent();
    }, [resizeToContent, value]);

    const handleChange = React.useCallback(
      (event: React.ChangeEvent<HTMLTextAreaElement>) => {
        resizeToContent();
        onChange?.(event);
      },
      [onChange, resizeToContent],
    );

    const handleInput = React.useCallback(
      (event: React.FormEvent<HTMLTextAreaElement>) => {
        resizeToContent();
        onInput?.(event);
      },
      [onInput, resizeToContent],
    );

    return (
      <Textarea
        ref={setRefs}
        className={cn("min-h-0 resize-none", className)}
        onChange={handleChange}
        onInput={handleInput}
        rows={rows ?? minRows}
        style={style}
        value={value}
        {...props}
      />
    );
  },
);

AutoResizeTextarea.displayName = "AutoResizeTextarea";
