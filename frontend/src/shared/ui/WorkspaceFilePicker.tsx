import { useRef, type RefObject } from "react";

import { formatBytes } from "../lib/format";
import { Icon } from "./Icons";

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
        className="sr-only workspace-file-picker-input"
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
        className={`workspace-file-picker${disabled ? " is-disabled" : ""}`}
        data-invalid={invalid ? "true" : undefined}
      >
        {buttonTrigger ? (
          // 外部已有显式 <label htmlFor={id}> 时使用按钮触发器，避免重复 Tab 停留在隐藏 input 上。
          <button
            className="workspace-file-picker-trigger"
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
            className="workspace-file-picker-trigger"
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
              ? "workspace-file-picker-name"
              : "workspace-file-picker-placeholder"
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
            className="workspace-file-picker-clear"
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
