import { useRef, type RefObject } from "react";

import { formatBytes } from "../lib/format";
import { Icon } from "./Icons";
import { cn } from "./utils";

interface WorkspaceFilePickerProps {
  accept?: string;
  buttonTrigger?: boolean;
  describedBy?: string;
  disabled?: boolean;
  file: File | null;
  focusOnOpen?: boolean;
  id: string;
  inputRef?: RefObject<HTMLInputElement>;
  invalid?: boolean;
  label: string;
  onChange: (file: File | null) => void;
  placeholder: string;
  triggerLabel: string;
}

export function WorkspaceFilePicker({
  accept,
  buttonTrigger = false,
  describedBy,
  disabled = false,
  file,
  focusOnOpen = false,
  id,
  inputRef,
  invalid = false,
  label,
  onChange,
  placeholder,
  triggerLabel,
}: WorkspaceFilePickerProps) {
  const fallbackInputRef = useRef<HTMLInputElement>(null);
  const resolvedInputRef = inputRef ?? fallbackInputRef;

  const clearFile = () => {
    if (disabled) {
      return;
    }
    if (resolvedInputRef.current) {
      resolvedInputRef.current.value = "";
    }
    onChange(null);
  };

  return (
    <>
      <input
        accept={accept}
        aria-describedby={describedBy}
        aria-invalid={invalid || undefined}
        aria-label={label}
        className="sr-only"
        data-autofocus={focusOnOpen ? true : undefined}
        disabled={disabled}
        id={id}
        onChange={(event) =>
          onChange(event.target.files?.[0] ?? null)
        }
        ref={resolvedInputRef}
        tabIndex={buttonTrigger ? -1 : undefined}
        type="file"
      />
      <div
        className={cn(
          "flex min-h-[38px] w-full min-w-0 items-center gap-[9px] rounded-lg border border-border-default py-[3px] pr-2 pl-1 transition-colors hover:border-border-strong focus-within:border-ring-focus focus-within:ring-2 focus-within:ring-ring-focus/20 data-[invalid=true]:border-danger data-[invalid=true]:bg-danger-bg",
          disabled ? "bg-muted opacity-70" : "bg-canvas",
        )}
        data-invalid={invalid ? "true" : undefined}
      >
        {buttonTrigger ? (
          // 外部已有显式 <label htmlFor={id}> 时使用按钮触发器，避免重复 Tab 停留在隐藏 input 上。
          <button
            className="inline-flex min-h-[30px] shrink-0 items-center gap-1.5 rounded-md border border-brand-border bg-brand-subtle px-2.5 text-xs font-semibold whitespace-nowrap text-brand transition-colors hover:enabled:bg-active disabled:cursor-not-allowed disabled:opacity-60"
            disabled={disabled}
            onClick={() => resolvedInputRef.current?.click()}
            type="button"
          >
            <Icon name="upload" size={14} />
            {triggerLabel}
          </button>
        ) : (
          <label
            aria-disabled={disabled || undefined}
            className="inline-flex min-h-[30px] shrink-0 cursor-pointer items-center gap-1.5 rounded-md border border-brand-border bg-brand-subtle px-2.5 text-xs font-semibold whitespace-nowrap text-brand transition-colors hover:bg-active aria-disabled:cursor-not-allowed aria-disabled:opacity-60"
            htmlFor={id}
          >
            <Icon name="upload" size={14} />
            {triggerLabel}
          </label>
        )}
        <span
          aria-live="polite"
          className={
            file
              ? "min-w-0 flex-1 truncate text-xs font-medium text-text-primary"
              : "min-w-0 flex-1 truncate text-xs text-text-tertiary"
          }
          title={file?.name}
        >
          {file
            ? `${file.name} (${formatBytes(file.size)})`
            : placeholder}
        </span>
        {file ? (
          <button
            aria-label="清除已选文件"
            className="inline-grid h-[26px] w-[26px] shrink-0 place-items-center rounded-md bg-transparent p-0 text-text-tertiary transition-colors hover:enabled:bg-muted hover:enabled:text-text-primary disabled:opacity-50"
            disabled={disabled}
            onClick={clearFile}
            type="button"
          >
            <Icon name="close" size={13} />
          </button>
        ) : null}
      </div>
    </>
  );
}
