import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  MarkdownSplitEditor,
  type MarkdownSplitEditorProps,
} from "./MarkdownSplitEditor";

function ControlledEditor(
  props: Omit<MarkdownSplitEditorProps, "onChange" | "value"> & {
    initialValue?: string;
  },
) {
  const { initialValue = "", ...rest } = props;
  const [value, setValue] = useState(initialValue);
  return <MarkdownSplitEditor {...rest} onChange={setValue} value={value} />;
}

function mockMatchMedia(matchesDesktop: boolean) {
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  let currentMatches = matchesDesktop;
  const mediaQueryList = {
    addEventListener: vi.fn(
      (_type: string, listener: (event: MediaQueryListEvent) => void) => {
        listeners.add(listener);
      },
    ),
    addListener: vi.fn(),
    dispatchEvent: vi.fn(),
    get matches() {
      return currentMatches;
    },
    media: "(min-width: 1024px)",
    onchange: null,
    removeEventListener: vi.fn(
      (_type: string, listener: (event: MediaQueryListEvent) => void) => {
        listeners.delete(listener);
      },
    ),
    removeListener: vi.fn(),
  };

  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => mediaQueryList),
  );

  return {
    setDesktop(next: boolean) {
      currentMatches = next;
      const event = { matches: next } as MediaQueryListEvent;
      listeners.forEach((listener) => listener(event));
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("MarkdownSplitEditor", () => {
  it("桌面端默认分栏（左编辑右预览），支持在仅编辑/分栏/仅预览三态间切换", async () => {
    mockMatchMedia(true);
    const user = userEvent.setup();

    render(
      <ControlledEditor
        id="doc-guide"
        initialValue={"# 标题\n\n**加粗内容**"}
        label="指南正文"
        maxLength={1000}
      />,
    );

    const splitTab = screen.getByRole("tab", { name: "分栏" });
    const editTab = screen.getByRole("tab", { name: "仅编辑" });
    const previewTab = screen.getByRole("tab", { name: "仅预览" });

    expect(splitTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText("指南正文")).toBeInTheDocument();
    const preview = screen.getByTestId("markdown-split-preview");
    expect(within(preview).getByRole("heading", { name: "标题" })).toBeInTheDocument();
    expect(within(preview).getByText("加粗内容")).toBeInTheDocument();

    // 切至仅编辑：隐藏预览区，保留编辑器
    await user.click(editTab);
    expect(editTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText("指南正文")).toBeInTheDocument();
    expect(
      screen.queryByTestId("markdown-split-preview"),
    ).not.toBeInTheDocument();

    // 切至仅预览：隐藏文本域并禁用排版工具栏按钮，保留预览区
    await user.click(previewTab);
    expect(previewTab).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByLabelText("指南正文")).not.toBeInTheDocument();
    expect(screen.getByTestId("markdown-split-preview")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "加粗" })).toBeDisabled();

    // 切回分栏：编辑与预览恢复同步展示
    await user.click(splitTab);
    expect(screen.getByLabelText("指南正文")).toBeInTheDocument();
    expect(screen.getByTestId("markdown-split-preview")).toBeInTheDocument();
  });

  it("视口从宽屏缩窄再恢复宽屏时自动在双栏与编辑/预览 Tabs 间无损切换", async () => {
    const media = mockMatchMedia(true);

    render(
      <ControlledEditor
        id="responsive-guide"
        initialValue="## 响应式正文"
        label="响应式正文"
      />,
    );

    expect(screen.getByRole("tab", { name: "分栏" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByTestId("markdown-split-preview")).toBeInTheDocument();

    // 缩窄到 < 1024px：自动折叠为编辑/预览 Tabs 且默认显示编辑页
    act(() => {
      media.setDesktop(false);
    });
    expect(screen.queryByRole("tab", { name: "分栏" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "编辑" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      screen.queryByTestId("markdown-split-preview"),
    ).not.toBeInTheDocument();

    // 恢复宽屏：无损回到分栏视图
    act(() => {
      media.setDesktop(true);
    });
    expect(screen.getByRole("tab", { name: "分栏" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByTestId("markdown-split-preview")).toBeInTheDocument();
  });

  it("窄屏（< 1024px）自动切换为「编辑 / 预览」Tabs，避免双栏横向挤压", async () => {
    mockMatchMedia(false);
    const user = userEvent.setup();

    render(
      <ControlledEditor
        id="mobile-guide"
        initialValue="## 移动端预览"
        label="移动端正文"
      />,
    );

    expect(screen.queryByRole("tab", { name: "分栏" })).not.toBeInTheDocument();
    const editTab = screen.getByRole("tab", { name: "编辑" });
    const previewTab = screen.getByRole("tab", { name: "预览" });

    expect(editTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText("移动端正文")).toBeInTheDocument();
    expect(
      screen.queryByTestId("markdown-split-preview"),
    ).not.toBeInTheDocument();

    await user.click(previewTab);
    expect(previewTab).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByLabelText("移动端正文")).not.toBeInTheDocument();
    expect(
      within(screen.getByTestId("markdown-split-preview")).getByRole(
        "heading",
        { name: "移动端预览" },
      ),
    ).toBeInTheDocument();
  });

  it("工具栏支持在光标/选区位置精准插入二级标题、加粗、列表、代码块与表格并恢复焦点", async () => {
    mockMatchMedia(true);
    const user = userEvent.setup();

    render(
      <ControlledEditor
        id="toolbar-guide"
        initialValue="核心结论"
        label="工具栏正文"
      />,
    );

    const textarea = screen.getByLabelText("工具栏正文") as HTMLTextAreaElement;
    // 选中「核心结论」点击加粗 -> 包裹为 **核心结论** 并恢复焦点与内部选区
    textarea.focus();
    textarea.setSelectionRange(0, 4);
    await user.click(screen.getByRole("button", { name: "加粗" }));
    expect(textarea).toHaveValue("**核心结论**");
    expect(textarea).toHaveFocus();
    expect(textarea.selectionStart).toBe(2);
    expect(textarea.selectionEnd).toBe(6);

    // 移到末尾插入二级标题、列表、代码块与 3 列 2 行表格
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    await user.click(screen.getByRole("button", { name: "插入二级标题" }));
    expect(textarea.value).toContain("\n## 二级标题");

    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    await user.click(screen.getByRole("button", { name: "无序列表" }));
    expect(textarea.value).toContain("\n- 列表项");

    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    await user.click(screen.getByRole("button", { name: "代码块" }));
    expect(textarea.value).toContain("\n```\n代码内容\n```");

    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    await user.click(screen.getByRole("button", { name: "插入表格" }));
    expect(textarea.value).toContain("| 列 1 | 列 2 | 列 3 |");
    expect(textarea.value).toContain("| 内容 4 | 内容 5 | 内容 6 |");

    // 实时预览区正确渲染 GFM 表格与安全过滤 HTML
    const preview = screen.getByTestId("markdown-split-preview");
    expect(within(preview).getByRole("table")).toBeInTheDocument();
  });

  it("按 Unicode 码点统计字数，超限时高亮红字并标记 aria-invalid；预览区过滤危险 HTML", async () => {
    mockMatchMedia(true);
    const user = userEvent.setup();

    render(
      <ControlledEditor
        id="limit-guide"
        initialValue=""
        label="限长正文"
        maxLength={5}
      />,
    );

    const textarea = screen.getByLabelText("限长正文");
    const counter = screen.getByTestId("markdown-char-count");
    expect(counter).toHaveTextContent("0 / 5 字符");
    expect(counter).toHaveAttribute("data-over-limit", "false");
    expect(textarea).not.toHaveAttribute("aria-invalid");

    // 4 个中文 + 1 个 emoji 代理对（UTF-16 长度为 6，但 Unicode 码点为 5，不应超限）
    fireEvent.change(textarea, { target: { value: "财务审核😀" } });
    expect(counter).toHaveTextContent("5 / 5 字符");
    expect(counter).toHaveAttribute("data-over-limit", "false");

    // 超出 5 码点后红字高亮并标记 aria-invalid="true"
    await user.type(textarea, "超");
    expect(counter).toHaveTextContent("6 / 5 字符");
    expect(counter).toHaveAttribute("data-over-limit", "true");
    expect(counter).toHaveClass("text-danger");
    expect(textarea).toHaveAttribute("aria-invalid", "true");

    // 验证预览区防 XSS：原始 script 标签与 javascript: 链接均被安全过滤
    fireEvent.change(textarea, {
      target: { value: "<script>alert(1)</script>[点我](javascript:alert(1))" },
    });
    const preview = screen.getByTestId("markdown-split-preview");
    expect(preview.querySelector("script")).toBeNull();
    expect(preview.querySelector("a")).toBeNull();
  });

  it("支持快捷导入 .md 文件并校验扩展名与空文件，重复选择同一 .md 文件仍触发回调", () => {
    mockMatchMedia(true);
    const onImport = vi.fn();

    render(
      <ControlledEditor
        id="import-guide"
        importTriggerLabel="导入 .md"
        label="导入正文"
        onImportMarkdownFile={onImport}
      />,
    );

    const fileInput = screen.getByLabelText("导入 .md") as HTMLInputElement;
    // 拒绝 .txt
    fireEvent.change(fileInput, {
      target: {
        files: [new File(["hello"], "notes.txt", { type: "text/plain" })],
      },
    });
    expect(onImport).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/仅支持导入 \.md/);

    // 拒绝空 .md 文件
    fireEvent.change(fileInput, {
      target: {
        files: [new File([], "empty.md", { type: "text/markdown" })],
      },
    });
    expect(onImport).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/为空/);

    // 接受大小写不敏感的 .MD，并支持重复选择同一文件再次触发导入
    const mdFile = new File(["# 制度指南"], "policy.MD", {
      type: "text/markdown",
    });
    fireEvent.change(fileInput, {
      target: { files: [mdFile] },
    });
    fireEvent.change(fileInput, {
      target: { files: [mdFile] },
    });
    expect(onImport).toHaveBeenCalledTimes(2);
    expect(onImport).toHaveBeenLastCalledWith(mdFile);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("支持光标位于文本中间精准插入；工具栏无变更操作后移动光标打字不被残留选区劫持", async () => {
    mockMatchMedia(true);
    const user = userEvent.setup();

    render(
      <ControlledEditor
        id="caret-guide"
        initialValue={"- 已有项\n结尾"}
        label="光标正文"
      />,
    );

    const textarea = screen.getByLabelText("光标正文") as HTMLTextAreaElement;
    // 1. 选中已有 "- 已有项" 行再点击「无序列表」：文本无变化，不应残留 pendingSelectionRef
    textarea.focus();
    textarea.setSelectionRange(0, 5);
    await user.click(screen.getByRole("button", { name: "无序列表" }));
    expect(textarea).toHaveValue("- 已有项\n结尾");
    expect(textarea).toHaveFocus();

    // 将光标移至末尾继续输入：应正常追加在末尾，不被拉回 0..5 旧选区
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    await user.type(textarea, "追加内容");
    expect(textarea).toHaveValue("- 已有项\n结尾追加内容");

    // 2. 光标置于文本中间插入加粗语法，精准落在光标处而非末尾
    textarea.setSelectionRange(2, 2);
    await user.click(screen.getByRole("button", { name: "加粗" }));
    expect(textarea).toHaveValue("- **文本**已有项\n结尾追加内容");
    expect(textarea.selectionStart).toBe(4);
    expect(textarea.selectionEnd).toBe(6);
  });

  it("大文档连续输入下零分配码点计数准确且延迟预览区正确收敛到最新正文", () => {
    mockMatchMedia(true);

    render(
      <ControlledEditor
        id="large-doc-guide"
        initialValue=""
        label="大文档正文"
        maxLength={20_000}
      />,
    );

    const textarea = screen.getByLabelText("大文档正文");
    const counter = screen.getByTestId("markdown-char-count");
    const repeatedParagraph = "财务制度规范😀\n\n".repeat(1_000);
    const largeDoc = `${repeatedParagraph}## 最终章节标题\n\n**关键结论收敛**`;

    fireEvent.change(textarea, { target: { value: largeDoc } });

    expect(counter).toHaveTextContent(
      `${Array.from(largeDoc).length.toLocaleString()} / 20,000 字符`,
    );
    expect(counter).toHaveAttribute("data-over-limit", "false");
    const preview = screen.getByTestId("markdown-split-preview");
    expect(
      within(preview).getByRole("heading", { name: "最终章节标题" }),
    ).toBeInTheDocument();
    expect(within(preview).getByText("关键结论收敛")).toBeInTheDocument();
  });
});
