import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MarkdownContent } from "../../shared/ui/MarkdownContent";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("安全 Markdown", () => {
  it("不执行原始 HTML，外链安全打开，图片只降级为链接", () => {
    const { container } = render(
      <MarkdownContent
        text={[
          "<script>window.__markdownExecuted = true</script>",
          "",
          '<img src="https://tracking.example/pixel.png" onerror="window.__markdownExecuted = true" />',
          "",
          "[外部资料](https://example.com/report)",
          "",
          "![趋势图](https://cdn.example.com/chart.png)",
          "",
          "[危险链接](javascript:alert(1))",
          "",
          "[协议相对外链](//evil.example/phish)",
        ].join("\n")}
      />,
    );

    expect(container.querySelector("script")).not.toBeInTheDocument();
    expect(container.querySelector("img")).not.toBeInTheDocument();
    expect(window).not.toHaveProperty("__markdownExecuted");

    const external = screen.getByRole("link", { name: "外部资料" });
    expect(external).toHaveAttribute("href", "https://example.com/report");
    expect(external).toHaveAttribute("target", "_blank");
    expect(external).toHaveAttribute("rel", "noopener noreferrer");

    const imageLink = screen.getByRole("link", { name: "图片：趋势图" });
    expect(imageLink).toHaveAttribute(
      "href",
      "https://cdn.example.com/chart.png",
    );
    expect(imageLink).toHaveAttribute("target", "_blank");
    expect(
      screen.queryByRole("link", { name: "危险链接" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "协议相对外链" }),
    ).not.toBeInTheDocument();
  });

  it("代码块展示语言标签、支持一键复制并在 2 秒后自动复原，GFM 表格带横向滚动容器", async () => {
    vi.useFakeTimers();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    const { container } = render(
      <MarkdownContent
        text={[
          "```typescript",
          "const total = 1280;",
          "console.log(total);",
          "```",
          "",
          "| 费用科目 | 金额 |",
          "| --- | --- |",
          "| 差旅费 | 1280 |",
        ].join("\n")}
      />,
    );

    expect(screen.getByText("typescript")).toBeInTheDocument();
    const copyButton = screen.getByRole("button", { name: "复制代码" });
    await act(async () => {
      fireEvent.click(copyButton);
    });

    expect(writeText).toHaveBeenCalledWith(
      "const total = 1280;\nconsole.log(total);",
    );
    expect(screen.getByRole("button", { name: "已复制" })).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(2_000);
    });
    expect(
      screen.getByRole("button", { name: "复制代码" }),
    ).toBeInTheDocument();

    const tableWrap = container.querySelector(".markdown-table-wrap");
    expect(tableWrap).not.toBeNull();
    expect(tableWrap?.className).toContain("overflow-x-auto");
    expect(screen.getByText("差旅费")).toBeInTheDocument();
    expect(screen.getByText("1280")).toBeInTheDocument();
  });

  it("剪贴板不可用时点击复制代码优雅降级而不崩溃", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: vi.fn().mockRejectedValue(new Error("permission denied")),
      },
    });
    const execCommand = vi.fn().mockReturnValue(false);
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: execCommand,
    });

    render(<MarkdownContent text={"```json\n{\"ok\":true}\n```"} />);

    const copyButton = screen.getByRole("button", { name: "复制代码" });
    await act(async () => {
      fireEvent.click(copyButton);
    });

    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(
      screen.getByRole("button", { name: "复制代码" }),
    ).toBeInTheDocument();
  });
});
