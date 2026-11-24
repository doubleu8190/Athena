import {
  createContext,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type PropsWithChildren,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import {
  FOCUSABLE_SELECTOR,
  getFocusableElements,
} from "./focus";
import { Icon } from "./Icons";
import { cn } from "./utils";

interface ModalProps extends PropsWithChildren {
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

const ModalDepthContext = createContext(-1);
const modalStack: ModalLayer[] = [];
const modalStackListeners = new Set<() => void>();
let nextModalOrder = 0;
// 记录首个弹窗打开前页面的原始 overflow；仅当所有活跃弹窗都关闭时才恢复，避免嵌套弹窗非 LIFO 卸载提前解锁或永久锁死滚动。
let savedBodyOverflow: string | null = null;

// 所有弹窗共享层级栈，只有栈顶弹窗处理关闭键、遮罩和焦点循环。
function notifyModalStack() {
  modalStackListeners.forEach((listener) => listener());
}

function isTopModal(modalId: string): boolean {
  const topModal = modalStack.reduce<ModalLayer | null>((top, layer) => {
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

function registerModal(layer: ModalLayer) {
  const wasEmpty = modalStack.length === 0;
  const existingIndex = modalStack.findIndex(
    (item) => item.id === layer.id,
  );
  if (existingIndex >= 0) {
    modalStack.splice(existingIndex, 1);
  }
  modalStack.push(layer);
  if (wasEmpty && modalStack.length > 0) {
    savedBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
  }
  notifyModalStack();
}

function unregisterModal(modalId: string) {
  const index = modalStack.findIndex((layer) => layer.id === modalId);
  if (index >= 0) {
    modalStack.splice(index, 1);
    if (modalStack.length === 0 && savedBodyOverflow !== null) {
      document.body.style.overflow = savedBodyOverflow;
      savedBodyOverflow = null;
    }
    notifyModalStack();
  }
}

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
  const titleId = useId();
  const descriptionId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  const closeDisabledRef = useRef(closeDisabled);
  const [isTopmost, setIsTopmost] = useState(false);

  useEffect(() => {
    onCloseRef.current = onClose;
    closeDisabledRef.current = closeDisabled;
  }, [closeDisabled, onClose]);

  useEffect(() => {
    if (!open) {
      return;
    }

    const updateTopmost = () => setIsTopmost(isTopModal(modalId));
    modalStackListeners.add(updateTopmost);
    registerModal({
      depth: modalDepth,
      id: modalId,
      order: ++nextModalOrder,
    });
    updateTopmost();

    return () => {
      modalStackListeners.delete(updateTopmost);
      unregisterModal(modalId);
    };
  }, [modalDepth, modalId, open]);

  useEffect(() => {
    if (!open) {
      return;
    }

    previousFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;

    const focusInitialControl = () => {
      const panel = panelRef.current;
      if (!isTopModal(modalId)) {
        return;
      }
      // 保留 data-autofocus 的显式优先级，兼容隐藏文件输入的既有程序化聚焦。
      const target =
        panel?.querySelector<HTMLElement>("[data-autofocus]") ??
        panel?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR);
      target?.focus();
    };
    const timer = window.setTimeout(focusInitialControl, 0);

    const handleKeyDown = (event: KeyboardEvent) => {
      if (!isTopModal(modalId)) {
        return;
      }
      if (event.key === "Escape") {
        const escapeOwner = (event.target as Element | null)?.closest?.(
          "[data-modal-escape-owner]",
        );
        if (escapeOwner) {
          // 弹层内的展开控件先消费 Escape，Modal 在后续按键中再关闭。
          return;
        }
        event.preventDefault();
        if (!closeDisabledRef.current) {
          onCloseRef.current();
        }
        return;
      }
      if (event.key !== "Tab" || !panelRef.current) {
        return;
      }

      // 焦点在本对话框内循环，避免键盘用户落到遮罩后的页面控件。
      const controls = getFocusableElements(panelRef.current);
      if (controls.length === 0) {
        event.preventDefault();
        panelRef.current.focus();
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
    };

    document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("keydown", handleKeyDown, true);
      previousFocusRef.current?.focus();
    };
  }, [modalId, open]);

  if (!open) {
    return null;
  }

  const requestClose = () => {
    if (isTopModal(modalId) && !closeDisabledRef.current) {
      onCloseRef.current();
    }
  };

  return createPortal(
    <ModalDepthContext.Provider value={modalDepth}>
      <div
        className="modal-backdrop fixed inset-0 z-[var(--z-dialog)] grid place-items-center overflow-y-auto bg-black/40 p-3 backdrop-blur-[2px] max-sm:items-end sm:px-[18px] sm:py-7"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) {
            requestClose();
          }
        }}
        role="presentation"
      >
        <div
          aria-describedby={description ? descriptionId : undefined}
          aria-labelledby={titleId}
          aria-modal={isTopmost ? "true" : undefined}
          className={cn(
            "modal-card flex flex-col overflow-hidden rounded-[14px] border border-border-default bg-canvas shadow-md",
            panelClassName,
          )}
          ref={panelRef}
          role="dialog"
          tabIndex={-1}
        >
          <header className="modal-header flex shrink-0 items-start justify-between gap-5 border-b border-border-subtle px-4 pt-5 pb-4 sm:px-[22px]">
            <div>
              <h2
                className="text-lg font-semibold tracking-tight text-text-primary"
                id={titleId}
              >
                {title}
              </h2>
              {description ? (
                <p
                  className="mt-1 text-xs leading-relaxed text-text-secondary"
                  id={descriptionId}
                >
                  {description}
                </p>
              ) : null}
            </div>
            <button
              aria-label="关闭弹窗"
              className="modal-close grid h-[31px] w-[31px] shrink-0 place-items-center rounded-[7px] bg-transparent text-text-tertiary transition-colors hover:enabled:bg-subtle hover:enabled:text-text-primary"
              disabled={closeDisabled}
              onClick={requestClose}
              type="button"
            >
              <Icon name="close" size={17} />
            </button>
          </header>
          <div className="modal-body flex min-h-0 min-w-0 flex-1 flex-col gap-[13px] overflow-y-auto px-4 py-[18px] sm:px-[22px]">
            {children}
          </div>
          {footer ? (
            <footer className="modal-footer flex min-w-0 shrink-0 flex-wrap justify-end gap-[9px] border-t border-border-subtle px-4 pt-3.5 pb-[18px] sm:px-[22px]">
              {footer}
            </footer>
          ) : null}
        </div>
      </div>
    </ModalDepthContext.Provider>,
    document.body,
  );
}

