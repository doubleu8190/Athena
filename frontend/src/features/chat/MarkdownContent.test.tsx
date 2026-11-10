import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MarkdownContent } from "./MarkdownContent";

describe("安全 Markdown", () => {
  it("不执行原始 HTML，外链安全打开，图片只降级为链接", () => {
    const { container } = render(
      <MarkdownContent
        text={[
          "<script>window.__markdownExecuted = true</script>",
          "",
          "<img src=\"https://tracking.example/pixel.png\" onerror=\"window.__markdownExecuted = true\" />",
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
});

