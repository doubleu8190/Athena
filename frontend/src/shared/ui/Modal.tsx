import {
  createContext,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type PropsWithChildren,
  type ReactNode,
} from "react";

import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "./Dialog";
import { Icon } from "./Icons";
import { cn } from "./utils";

export interface ModalProps extends PropsWithChildren {
  closeDisabled?: boolean;
  description?: string;
  footer?: ReactNode;
  onClose: () => void;
  open: boolean;
  panelClassName?: string;
  title: string;
}

interface ModalLayer {
  depth: number;
  id: string;
  order: number;
}

// 维护嵌套深度与挂载顺序，确保即使父子 Modal 在同一 commit 中同时以 open={true} 挂载，
// 深度更大的内层弹窗也始终判定为栈顶，避免 useLayoutEffect 子先父后执行造成死锁。
const ModalDepthContext = createContext(-1);
const openModalStack: ModalLayer[] = [];
const modalStackListeners = new Set<() => void>();
let nextModalOrder = 0;

function notifyModalStack(): void {
  modalStackListeners.forEach((listener) => listener());
}

function isTopModal(modalId: string): boolean {
  const topModal = openModalStack.reduce<ModalLayer | null>((top, layer) => {
    if (
      !top ||
      layer.depth > top.depth ||
      (layer.depth === top.depth && layer.order > top.order)
    ) {
      return layer;
    }
    return top;
  }, null);
  return topModal?.id === modalId;
}

const ENABLED_TABBABLE_SELECTOR = [
  'button:not(:disabled):not([tabindex="-1"])',
  'a[href]:not([tabindex="-1"])',
  'input:not(:disabled):not([type="hidden"]):not([tabindex="-1"])',
  'select:not(:disabled):not([tabindex="-1"])',
  'textarea:not(:disabled):not([tabindex="-1"])',
  '[tabindex]:not([tabindex="-1"]):not(:disabled)',
].join(",");

/**
 * 兜底修复 Radix FocusScope 未识别 `<fieldset disabled>` 后代控件的已知边界：
 * 仅在弹窗内存在禁用 fieldset 且焦点位于真实首/尾可聚焦控件时接管 Tab 循环。
 */
function handleFieldsetDisabledTabLoop(
  event: KeyboardEvent<HTMLDivElement>,
): void {
  if (event.key !== "Tab" || event.altKey || event.ctrlKey || event.metaKey) {
    return;
  }
  const container = event.currentTarget;
  if (!container.querySelector("fieldset:disabled")) {
    return;
  }
  const controls = Array.from(
    container.querySelectorAll<HTMLElement>(ENABLED_TABBABLE_SELECTOR),
  );
  if (controls.length === 0) {
    return;
  }
  const first = controls[0];
  const last = controls[controls.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

/**
 * 业务弹窗统一封装，底层复用 Radix Dialog（Dialog.tsx）的焦点陷阱、Escape 层级栈、
 * 遮罩外部点击关闭与滚动锁定能力，保留既有 Modal Props、业务宽度类名与受控焦点归还语义。
 */
export function Modal({
  children,
  closeDisabled = false,
  description,
  footer,
  onClose,
  open,
  panelClassName,
  title,
}: ModalProps) {
  const parentDepth = useContext(ModalDepthContext);
  const modalDepth = parentDepth + 1;
  const modalId = useId();
  const contentRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const [isTopmost, setIsTopmost] = useState(false);

  useLayoutEffect(() => {
    if (!open) {
      setIsTopmost(false);
      return;
    }

    // 在子组件 Radix FocusScope 的 useEffect 移动焦点之前，同步记录触发元素。
    previousFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;

    const updateTopmost = () => setIsTopmost(isTopModal(modalId));
    modalStackListeners.add(updateTopmost);
    const existingIndex = openModalStack.findIndex(
      (layer) => layer.id === modalId,
    );
    if (existingIndex >= 0) {
      openModalStack.splice(existingIndex, 1);
    }
    openModalStack.push({
      depth: modalDepth,
      id: modalId,
      order: ++nextModalOrder,
    });
    notifyModalStack();

    return () => {
      modalStackListeners.delete(updateTopmost);
      const index = openModalStack.findIndex((layer) => layer.id === modalId);
      if (index >= 0) {
        openModalStack.splice(index, 1);
        notifyModalStack();
      }
    };
  }, [modalDepth, modalId, open]);

  useEffect(() => {
    if (!open) {
      return;
    }

    // 子组件 FocusScope 的 useEffect cleanup 会先于此处解除焦点陷阱，
    // 在此同步归还焦点可避免被尚未卸载的 FocusScope focusin 监听器抢回后跌落到 body。
    return () => {
      const target = previousFocusRef.current;
      previousFocusRef.current = null;
      if (target?.isConnected) {
        target.focus();
      }
    };
  }, [open]);

  const requestClose = () => {
    if (isTopModal(modalId) && !closeDisabled) {
      onClose();
    }
  };

  return (
    <Dialog
      onOpenChange={(nextOpen) => {
        if (!nextOpen) {
          requestClose();
        }
      }}
      open={open}
    >
      <DialogContent
        {...(description ? {} : { "aria-describedby": undefined })}
        aria-modal={isTopmost ? "true" : undefined}
        className={cn(
          "modal-card overflow-hidden rounded-[14px]",
          panelClassName,
        )}
        closeDisabled={closeDisabled}
        hideCloseButton
        onCloseAutoFocus={(event) => {
          // 阻止 Radix 在无 DialogTrigger 时将焦点重置到 body，交由上方 cleanup 精确归还。
          event.preventDefault();
        }}
        onEscapeKeyDown={(event) => {
          if (!isTopModal(modalId)) {
            event.preventDefault();
            return;
          }
          // 兼容声明自管 Escape 的嵌套控件（如展开中的 Select 触发器），让其优先消费按键。
          const escapeOwner = (event.target as Element | null)?.closest?.(
            "[data-modal-escape-owner]",
          );
          if (escapeOwner) {
            event.preventDefault();
          }
        }}
        onInteractOutside={(event) => {
          if (!isTopModal(modalId)) {
            event.preventDefault();
          }
        }}
        onKeyDown={handleFieldsetDisabledTabLoop}
        onOpenAutoFocus={(event) => {
          // 保留 data-autofocus 的显式聚焦优先级，兼容隐藏文件输入与表单首字段聚焦。
          const target =
            contentRef.current?.querySelector<HTMLElement>("[data-autofocus]");
          if (target) {
            event.preventDefault();
            target.focus();
          }
        }}
        onPointerDownOutside={(event) => {
          if (!isTopModal(modalId)) {
            event.preventDefault();
          }
        }}
        ref={contentRef}
        size="none"
      >
        <header className="modal-header flex shrink-0 items-start justify-between gap-5 border-b border-border-subtle px-4 pt-5 pb-4 sm:px-[22px]">
          <div>
            <DialogTitle className="text-lg font-semibold tracking-tight text-text-primary">
              {title}
            </DialogTitle>
            {description ? (
              <DialogDescription className="mt-1 text-xs leading-relaxed text-text-secondary">
                {description}
              </DialogDescription>
            ) : null}
          </div>
          <DialogClose asChild>
            <button
              aria-label="关闭弹窗"
              className="modal-close grid h-[31px] w-[31px] shrink-0 place-items-center rounded-[7px] bg-transparent text-text-tertiary transition-colors hover:enabled:bg-subtle hover:enabled:text-text-primary disabled:pointer-events-none disabled:opacity-40"
              disabled={closeDisabled}
              type="button"
            >
              <Icon name="close" size={17} />
            </button>
          </DialogClose>
        </header>
        <ModalDepthContext.Provider value={modalDepth}>
          <div className="modal-body flex min-h-0 min-w-0 flex-1 flex-col gap-[13px] overflow-y-auto px-4 py-[18px] sm:px-[22px]">
            {children}
          </div>
          {footer ? (
            <footer className="modal-footer flex min-w-0 shrink-0 flex-wrap justify-end gap-[9px] border-t border-border-subtle px-4 pt-3.5 pb-[18px] sm:px-[22px]">
              {footer}
            </footer>
          ) : null}
        </ModalDepthContext.Provider>
      </DialogContent>
    </Dialog>
  );
}
