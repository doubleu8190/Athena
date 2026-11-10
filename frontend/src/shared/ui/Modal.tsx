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

import { Icon } from "./Icons";

interface ModalProps extends PropsWithChildren {
  closeDisabled?: boolean;
  description?: string;
  footer?: ReactNode;
  onClose: () => void;
  open: boolean;
  panelClassName?: string;
  title: string;
}

const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "a[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

interface ModalLayer {
  depth: number;
  id: string;
  order: number;
}

const ModalDepthContext = createContext(-1);
const modalStack: ModalLayer[] = [];
const modalStackListeners = new Set<() => void>();
let nextModalOrder = 0;

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
  const existingIndex = modalStack.findIndex(
    (item) => item.id === layer.id,
  );
  if (existingIndex >= 0) {
    modalStack.splice(existingIndex, 1);
  }
  modalStack.push(layer);
  notifyModalStack();
}

function unregisterModal(modalId: string) {
  const index = modalStack.findIndex((layer) => layer.id === modalId);
  if (index >= 0) {
    modalStack.splice(index, 1);
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
    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const focusInitialControl = () => {
      const panel = panelRef.current;
      if (!isTopModal(modalId)) {
        return;
      }
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
      const controls = Array.from(
        panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      );
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
      document.body.style.overflow = originalOverflow;
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
        className="modal-backdrop"
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
          className={`modal-card${panelClassName ? ` ${panelClassName}` : ""}`}
          ref={panelRef}
          role="dialog"
          tabIndex={-1}
        >
          <header className="modal-header">
            <div>
              <h2 id={titleId}>{title}</h2>
              {description ? <p id={descriptionId}>{description}</p> : null}
            </div>
            <button
              aria-label="关闭弹窗"
              className="modal-close"
              disabled={closeDisabled}
              onClick={requestClose}
              type="button"
            >
              <Icon name="close" size={17} />
            </button>
          </header>
          <div className="modal-body">{children}</div>
          {footer ? <footer className="modal-footer">{footer}</footer> : null}
        </div>
      </div>
    </ModalDepthContext.Provider>,
    document.body,
  );
}

