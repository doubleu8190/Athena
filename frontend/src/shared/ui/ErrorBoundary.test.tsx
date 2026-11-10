import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "./ErrorBoundary";

function BrokenView(): never {
  throw new Error("render failed with private data");
}

describe("全局错误边界", () => {
  it("捕获渲染异常并只展示安全兜底内容", () => {
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});

    render(
      <ErrorBoundary>
        <BrokenView />
      </ErrorBoundary>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "页面遇到意外错误",
    );
    expect(screen.getByRole("button", { name: "重新加载页面" })).toBeVisible();
    expect(
      screen.queryByText(/render failed with private data/),
    ).not.toBeInTheDocument();
    consoleError.mockRestore();
  });
});
