import { ShieldCheck } from "lucide-react";
import {
  Component,
  type ErrorInfo,
  type ReactNode,
} from "react";

import { Brand } from "./Brand";
import { Button } from "./Button";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  failed: boolean;
}

export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 只在开发控制台保留诊断信息，页面本身不回显堆栈或可能含业务数据的异常文本。
    console.error(
      "Athena UI rendering failed",
      error.name,
      info.componentStack,
    );
  }

  render() {
    if (!this.state.failed) {
      return this.props.children;
    }
    return (
      <main className="flex min-h-dvh flex-col items-center justify-center gap-6 bg-subtle px-4 py-8 sm:px-7">
        <Brand />
        <section
          aria-labelledby="global-error-title"
          className="w-full max-w-[540px] rounded-2xl border border-border-default bg-canvas px-6 py-8 text-center shadow-md sm:px-9 sm:py-9"
          role="alert"
        >
          <span
            aria-hidden="true"
            className="mx-auto mb-4 grid h-12 w-12 place-items-center rounded-xl border border-danger-border bg-danger-bg text-danger"
          >
            <ShieldCheck
              aria-hidden="true"
              focusable="false"
              size={22}
              strokeWidth={1.7}
            />
          </span>
          <h1
            className="text-2xl font-semibold tracking-tight text-text-primary sm:text-[26px]"
            id="global-error-title"
          >
            页面遇到意外错误
          </h1>
          <p className="mx-auto mt-2.5 mb-6 max-w-md text-sm leading-relaxed text-text-secondary">
            当前页面无法继续安全渲染。请重新加载；如果问题持续出现，请稍后再试。
          </p>
          <Button
            onClick={() => window.location.reload()}
            type="button"
            variant="primary"
          >
            重新加载页面
          </Button>
        </section>
      </main>
    );
  }
}
