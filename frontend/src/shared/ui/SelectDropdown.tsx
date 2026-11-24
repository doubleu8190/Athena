import { useEffect, useRef, useState } from "react";

import { Icon } from "./Icons";

export interface SelectDropdownOption {
  badge?: string;
  disabled?: boolean;
  label: string;
  value: string;
}

export interface SelectDropdownProps {
  classPrefix: string;
  disabled?: boolean;
  id: string;
  label: string;
  listboxLabel: string;
  onChange: (value: string) => void;
  options: readonly SelectDropdownOption[];
  placeholder?: string;
  value: string;
}

function findNextEnabledIndex(
  options: readonly SelectDropdownOption[],
  startIndex: number,
  direction: 1 | -1,
): number {
  if (
    options.length === 0 ||
    startIndex < 0 ||
    startIndex >= options.length
  ) {
    return -1;
  }
  for (
    let index = startIndex;
    index >= 0 && index < options.length;
    index += direction
  ) {
    if (!options[index].disabled) {
      return index;
    }
  }
  return -1;
}

// 通用无障碍下拉选择控件（ARIA combobox + listbox），支持键盘导航、点击外部关闭及嵌套 Modal 下的 Escape 分层消费。
export function SelectDropdown({
  classPrefix,
  disabled = false,
  id,
  label,
  listboxLabel,
  onChange,
  options,
  placeholder = "",
  value,
}: SelectDropdownProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);

  const selectedIndex = Math.max(
    0,
    options.findIndex((option) => option.value === value),
  );
  const [activeValue, setActiveValue] = useState(
    () => options[selectedIndex]?.value ?? "",
  );

  // 选项列表异步重排或缩短后，确保当前激活项始终指向有效选项。
  const effectiveActiveOption =
    options.find((option) => option.value === activeValue && !option.disabled) ??
    options.find((option) => option.value === value && !option.disabled) ??
    options.find((option) => !option.disabled) ??
    options[0];
  const effectiveActiveValue = effectiveActiveOption?.value ?? "";
  const effectiveActiveIndex = Math.max(
    0,
    options.findIndex((option) => option.value === effectiveActiveValue),
  );
  const selected = options.find((option) => option.value === value);
  const activeOptionId = `${id}-option-${effectiveActiveIndex}`;

  useEffect(() => {
    if (!options.some((option) => option.value === activeValue)) {
      // 列表刷新移除了当前高亮项时，回退至当前已选项或首项，避免 Enter 落到失效项。
      setActiveValue(
        options[selectedIndex]?.value ?? options[0]?.value ?? "",
      );
    }
  }, [activeValue, options, selectedIndex]);

  useEffect(() => {
    if (disabled && open) {
      setOpen(false);
    }
  }, [disabled, open]);

  useEffect(() => {
    if (!open || options.length === 0) {
      return;
    }
    const activeOption = rootRef.current?.querySelector<HTMLElement>(
      `[id="${activeOptionId}"]`,
    );
    activeOption?.scrollIntoView?.({ block: "nearest" });
  }, [activeOptionId, open, options.length]);

  useEffect(() => {
    if (!open) {
      return;
    }
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    // 仅在点击浮层外部时收起菜单，内部选项点击由选项按钮自身处理。
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
    };
  }, [open]);

  const openAt = (index: number, direction: 1 | -1 = 1) => {
    if (disabled || options.length === 0) {
      return;
    }
    const boundedIndex = Math.min(options.length - 1, Math.max(0, index));
    const forwardIndex = findNextEnabledIndex(options, boundedIndex, direction);
    const enabledIndex =
      forwardIndex >= 0
        ? forwardIndex
        : findNextEnabledIndex(options, 0, 1);
    const targetOption =
      enabledIndex >= 0 ? options[enabledIndex] : options[selectedIndex];
    if (targetOption) {
      setActiveValue(targetOption.value);
    }
    setOpen(true);
  };

  // 菜单已展开时沿方向移动高亮；若目标方向已无可用选项（含首尾存在禁用项边界）则原地停留，不回跳到已选项。
  const moveActive = (direction: 1 | -1) => {
    const nextIndex = findNextEnabledIndex(
      options,
      effectiveActiveIndex + direction,
      direction,
    );
    if (nextIndex >= 0) {
      setActiveValue(options[nextIndex].value);
    }
  };

  const commitOption = (option: SelectDropdownOption | undefined) => {
    if (!option || option.disabled) {
      return;
    }
    onChange(option.value);
    setOpen(false);
  };

  return (
    <div className={`${classPrefix} relative min-w-0`} ref={rootRef}>
      <button
        aria-activedescendant={
          open && options.length > 0 ? activeOptionId : undefined
        }
        aria-controls={`${id}-listbox`}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={label}
        className={`${classPrefix}-trigger flex min-h-[38px] w-full min-w-0 items-center justify-between gap-2.5 rounded-lg border border-border-default bg-canvas px-[11px] text-left text-[13px] font-medium text-text-primary transition-colors outline-hidden hover:enabled:border-border-strong focus-visible:border-ring-focus focus-visible:ring-2 focus-visible:ring-ring-focus/20 aria-expanded:border-ring-focus aria-expanded:ring-2 aria-expanded:ring-ring-focus/20 disabled:cursor-not-allowed disabled:bg-muted disabled:text-text-secondary disabled:opacity-70`}
        data-modal-escape-owner={open ? "self" : undefined}
        disabled={disabled}
        id={id}
        onBlur={(event) => {
          if (!rootRef.current?.contains(event.relatedTarget as Node | null)) {
            setOpen(false);
          }
        }}
        onClick={() => (open ? setOpen(false) : openAt(selectedIndex))}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            if (open) {
              moveActive(1);
            } else {
              openAt(selectedIndex, 1);
            }
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            if (open) {
              moveActive(-1);
            } else {
              openAt(selectedIndex, -1);
            }
          } else if (event.key === "Home" && open && options.length > 0) {
            event.preventDefault();
            const firstEnabled = findNextEnabledIndex(options, 0, 1);
            if (firstEnabled >= 0) {
              setActiveValue(options[firstEnabled].value);
            }
          } else if (event.key === "End" && open && options.length > 0) {
            event.preventDefault();
            const lastEnabled = findNextEnabledIndex(
              options,
              options.length - 1,
              -1,
            );
            if (lastEnabled >= 0) {
              setActiveValue(options[lastEnabled].value);
            }
          } else if ((event.key === "Enter" || event.key === " ") && open) {
            event.preventDefault();
            // 使用对象引用而非真值判空，保证空字符串 value（如“未归属项目”）也可通过键盘提交。
            commitOption(effectiveActiveOption);
          } else if (event.key === "Escape" && open) {
            event.preventDefault();
            setOpen(false);
          }
        }}
        role="combobox"
        type="button"
      >
        <span className={`${classPrefix}-value min-w-0 truncate`}>
          {selected?.label ?? placeholder}
        </span>
        <Icon
          className={`${classPrefix}-chevron shrink-0 text-text-tertiary transition-transform duration-150 ${open ? "-rotate-90" : "rotate-90"}`}
          name="chevron"
          size={12}
        />
      </button>
      {open ? (
        <div
          aria-label={listboxLabel}
          className={`${classPrefix}-menu absolute top-[calc(100%+6px)] right-0 left-0 z-[var(--z-popover)] grid max-h-[min(240px,40vh)] gap-[3px] overflow-y-auto rounded-[10px] border border-border-default bg-canvas p-[5px] shadow-md`}
          id={`${id}-listbox`}
          role="listbox"
        >
          {options.map((option, index) => (
            <button
              aria-disabled={option.disabled || undefined}
              aria-selected={option.value === value}
              className={`${classPrefix}-option flex min-h-[34px] min-w-0 items-center justify-between gap-2 rounded-[7px] bg-transparent px-[9px] py-1 text-left text-xs text-text-primary outline-hidden hover:bg-brand-subtle focus-visible:bg-brand-subtle data-[active=true]:bg-brand-subtle aria-selected:bg-brand-subtle aria-selected:font-semibold aria-selected:text-brand disabled:opacity-50`}
              data-active={option.value === effectiveActiveValue}
              disabled={option.disabled}
              id={`${id}-option-${index}`}
              key={option.value === "" ? "__empty__" : option.value}
              onClick={() => commitOption(option)}
              onMouseDown={(event) => {
                // 保持焦点留在触发器，选择后仍可继续使用键盘操作。
                event.preventDefault();
              }}
              onMouseMove={() => {
                if (!option.disabled) {
                  setActiveValue(option.value);
                }
              }}
              role="option"
              tabIndex={-1}
              type="button"
            >
              <span
                className={`${classPrefix}-option-label min-w-0 flex-1 truncate`}
              >
                {option.label}
              </span>
              {option.badge ? (
                <span
                  className={`${classPrefix}-option-badge shrink-0 rounded-full border border-warning-border bg-warning-bg px-1.5 py-px text-xs font-semibold whitespace-nowrap text-warning`}
                >
                  {option.badge}
                </span>
              ) : null}
              {option.value === value ? (
                <span
                  aria-hidden="true"
                  className={`${classPrefix}-check shrink-0 font-bold text-brand-hover`}
                >
                  ✓
                </span>
              ) : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
