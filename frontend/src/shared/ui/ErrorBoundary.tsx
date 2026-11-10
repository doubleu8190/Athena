import {
  Component,
  type ErrorInfo,
  type ReactNode,
} from "react";

import { Brand } from "./Brand";
import { Icon } from "./Icons";

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
      <main className="error-boundary-page">
        <Brand />
        <section
          aria-labelledby="global-error-title"
          className="error-boundary-card"
          role="alert"
        >
          <span className="error-boundary-icon" aria-hidden="true">
            <Icon name="shield" size={24} />
          </span>
          <p className="page-eyebrow">Unexpected UI error</p>
          <h1 id="global-error-title">页面遇到意外错误</h1>
          <p>
            当前页面无法继续安全渲染。请重新加载；如果问题持续出现，请稍后再试。
          </p>
          <button
            className="button-primary"
            onClick={() => window.location.reload()}
            type="button"
          >
            重新加载页面
          </button>
        </section>
      </main>
    );
  }
}
