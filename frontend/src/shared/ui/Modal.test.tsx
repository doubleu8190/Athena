import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { Modal } from "./Modal";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./Select";

function ModalHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)} type="button">
        打开弹窗
      </button>
      <Modal
        footer={
          <>
            <button onClick={() => setOpen(false)} type="button">
              取消
            </button>
            <button type="button">保存</button>
          </>
        }
        onClose={() => setOpen(false)}
        open={open}
        title="编辑名称"
      >
        <label>
          名称
          <input data-autofocus />
        </label>
      </Modal>
    </>
  );
}

function NestedModalHarness() {
  const [outerOpen, setOuterOpen] = useState(false);
  const [innerOpen, setInnerOpen] = useState(false);
  const [innerCloseDisabled, setInnerCloseDisabled] = useState(false);

  return (
    <>
      <button onClick={() => setOuterOpen(true)} type="button">
        打开外层弹窗
      </button>
      <Modal
        onClose={() => setOuterOpen(false)}
        open={outerOpen}
        title="外层弹窗"
      >
        <button onClick={() => setInnerOpen(true)} type="button">
          打开内层弹窗
        </button>
        <label>
          外层输入
          <input />
        </label>
        <Modal
          closeDisabled={innerCloseDisabled}
          onClose={() => setInnerOpen(false)}
          open={innerOpen}
          title="内层弹窗"
        >
          <label>
            内层输入
            <input data-autofocus />
          </label>
          <button
            onClick={() => setInnerCloseDisabled((value) => !value)}
            type="button"
          >
            切换请求中
          </button>
        </Modal>
      </Modal>
    </>
  );
}

describe("Modal", () => {
  it("打开后聚焦内容，Escape 关闭并把焦点还给触发按钮", async () => {
    const user = userEvent.setup();
    render(<ModalHarness />);
    const trigger = screen.getByRole("button", { name: "打开弹窗" });

    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "编辑名称" });
    const input = screen.getByLabelText("名称");
    await waitFor(() => expect(input).toHaveFocus());
    expect(dialog).toHaveAttribute("aria-modal", "true");

    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: "编辑名称" }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("嵌套弹窗中只有最上层响应 Escape，并逐层恢复焦点", async () => {
    const user = userEvent.setup();
    render(<NestedModalHarness />);
    const outerTrigger = screen.getByRole("button", {
      name: "打开外层弹窗",
    });

    await user.click(outerTrigger);
    const outer = screen.getByRole("dialog", { name: "外层弹窗" });
    const openInner = within(outer).getByRole("button", {
      name: "打开内层弹窗",
    });
    await user.click(openInner);

    const inner = screen.getByRole("dialog", { name: "内层弹窗" });
    await waitFor(() =>
      expect(within(inner).getByLabelText("内层输入")).toHaveFocus(),
    );
    expect(inner).toHaveAttribute("aria-modal", "true");
    expect(outer).not.toHaveAttribute("aria-modal");

    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: "内层弹窗" }),
    ).not.toBeInTheDocument();
    expect(outer).toBeInTheDocument();
    await waitFor(() => expect(openInner).toHaveFocus());
    expect(outer).toHaveAttribute("aria-modal", "true");

    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: "外层弹窗" }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(outerTrigger).toHaveFocus());
  });

  it("内层打开时外层关闭按钮和遮罩不会关闭任一层", async () => {
    const user = userEvent.setup();
    render(<NestedModalHarness />);

    await user.click(
      screen.getByRole("button", { name: "打开外层弹窗" }),
    );
    const outer = screen.getByRole("dialog", { name: "外层弹窗" });
    await user.click(
      within(outer).getByRole("button", { name: "打开内层弹窗" }),
    );
    const inner = screen.getByRole("dialog", { name: "内层弹窗" });

    // 内层打开时，Radix Dialog 会对外层施加 aria-hidden 与 pointer-events: none 隔离；
    // 即使程序化触发外层关闭按钮，也不会关闭任一层。
    expect(outer).toHaveAttribute("aria-hidden", "true");
    fireEvent.click(
      within(outer).getByRole("button", { name: "关闭弹窗", hidden: true }),
    );
    expect(inner).toBeInTheDocument();
    expect(outer).toBeInTheDocument();

    // 点击遮罩/外部区域时仅关闭最顶层的内层弹窗，外层弹窗保持打开。
    fireEvent.pointerDown(document.body);
    expect(
      screen.queryByRole("dialog", { name: "内层弹窗" }),
    ).not.toBeInTheDocument();
    expect(outer).toBeInTheDocument();
  });

  it("内层请求中时外层也不能通过 Escape、关闭按钮或遮罩卸载表单", async () => {
    const user = userEvent.setup();
    render(<NestedModalHarness />);

    await user.click(
      screen.getByRole("button", { name: "打开外层弹窗" }),
    );
    const outer = screen.getByRole("dialog", { name: "外层弹窗" });
    await user.click(
      within(outer).getByRole("button", { name: "打开内层弹窗" }),
    );
    const inner = screen.getByRole("dialog", { name: "内层弹窗" });
    const innerClose = within(inner).getByRole("button", {
      name: "关闭弹窗",
    });
    await user.click(
      within(inner).getByRole("button", { name: "切换请求中" }),
    );
    await waitFor(() => expect(innerClose).toBeDisabled());

    await user.keyboard("{Escape}");
    fireEvent.click(
      within(outer).getByRole("button", { name: "关闭弹窗", hidden: true }),
    );
    fireEvent.pointerDown(document.body);
    expect(inner).toBeInTheDocument();
    expect(outer).toBeInTheDocument();

    await user.click(
      within(inner).getByRole("button", { name: "切换请求中" }),
    );
    await waitFor(() => expect(innerClose).not.toBeDisabled());
    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: "内层弹窗" }),
    ).not.toBeInTheDocument();
    expect(outer).toBeInTheDocument();
  });

  it("焦点循环只发生在最上层弹窗内", async () => {
    const user = userEvent.setup();
    render(<NestedModalHarness />);

    await user.click(
      screen.getByRole("button", { name: "打开外层弹窗" }),
    );
    const outer = screen.getByRole("dialog", { name: "外层弹窗" });
    await user.click(
      within(outer).getByRole("button", { name: "打开内层弹窗" }),
    );
    const inner = screen.getByRole("dialog", { name: "内层弹窗" });
    const innerClose = within(inner).getByRole("button", {
      name: "关闭弹窗",
    });
    const lastControl = within(inner).getByRole("button", {
      name: "切换请求中",
    });
    await waitFor(() =>
      expect(within(inner).getByLabelText("内层输入")).toHaveFocus(),
    );

    lastControl.focus();
    await user.tab();
    expect(innerClose).toHaveFocus();
    await user.tab({ shift: true });
    expect(lastControl).toHaveFocus();
    expect(
      within(outer).getByRole("button", { name: "打开内层弹窗", hidden: true }),
    ).not.toHaveFocus();
  });

  it("焦点循环跳过程序化和禁用控件", async () => {
    const user = userEvent.setup();
    render(
      <Modal onClose={() => undefined} open title="上传文件">
        <button data-autofocus type="button">
          最后的可见控件
        </button>
        <input
          aria-label="程序化文件输入"
          tabIndex={-1}
          type="file"
        />
        <button tabIndex={-1} type="button">
          程序化按钮
        </button>
        <button disabled type="button">
          直接禁用按钮
        </button>
        <fieldset disabled>
          <button type="button">字段集禁用按钮</button>
        </fieldset>
      </Modal>,
    );

    const lastVisibleControl = screen.getByRole("button", {
      name: "最后的可见控件",
    });
    const hiddenInput = screen.getByLabelText("程序化文件输入");
    const programmaticButton = screen.getByRole("button", {
      name: "程序化按钮",
    });
    const disabledButton = screen.getByRole("button", {
      name: "直接禁用按钮",
    });
    const fieldsetDisabledButton = screen.getByRole("button", {
      name: "字段集禁用按钮",
    });
    const firstControl = screen.getByRole("button", {
      name: "关闭弹窗",
    });
    await waitFor(() => expect(lastVisibleControl).toHaveFocus());

    await user.tab();
    expect(firstControl).toHaveFocus();
    expect(hiddenInput).not.toHaveFocus();
    expect(programmaticButton).not.toHaveFocus();
    expect(disabledButton).not.toHaveFocus();
    expect(fieldsetDisabledButton).not.toHaveFocus();
  });

  it("嵌套或并发弹窗按非 LIFO 顺序卸载时保持滚动锁定，全部关闭后才恢复原始 overflow", async () => {
    const user = userEvent.setup();
    document.body.style.overflow = "auto";

    function NonLifoModalHarness() {
      const [firstOpen, setFirstOpen] = useState(true);
      const [secondOpen, setSecondOpen] = useState(false);
      return (
        <>
          <Modal
            onClose={() => setFirstOpen(false)}
            open={firstOpen}
            title="弹窗 A"
          >
            <button onClick={() => setSecondOpen(true)} type="button">
              打开弹窗 B
            </button>
          </Modal>
          <Modal
            onClose={() => setSecondOpen(false)}
            open={secondOpen}
            title="弹窗 B"
          >
            <button onClick={() => setFirstOpen(false)} type="button">
              从 B 中关闭 A
            </button>
            <button onClick={() => setSecondOpen(false)} type="button">
              关闭 B
            </button>
          </Modal>
        </>
      );
    }

    try {
      render(<NonLifoModalHarness />);
      expect(document.body).toHaveAttribute("data-scroll-locked", "1");

      await user.click(screen.getByRole("button", { name: "打开弹窗 B" }));
      expect(document.body).toHaveAttribute("data-scroll-locked", "2");

      // 先关闭较早挂载的弹窗 A（非 LIFO），弹窗 B 仍打开，body 必须维持滚动锁定。
      await user.click(
        screen.getByRole("button", { name: "从 B 中关闭 A" }),
      );
      expect(
        screen.queryByRole("dialog", { name: "弹窗 A" }),
      ).not.toBeInTheDocument();
      expect(
        screen.getByRole("dialog", { name: "弹窗 B" }),
      ).toBeInTheDocument();
      expect(document.body).toHaveAttribute("data-scroll-locked", "1");

      // 最后一个活跃弹窗 B 关闭后，解除滚动锁定并保持原始 overflow（"auto"）。
      await user.click(screen.getByRole("button", { name: "关闭 B" }));
      expect(
        screen.queryByRole("dialog", { name: "弹窗 B" }),
      ).not.toBeInTheDocument();
      expect(document.body).not.toHaveAttribute("data-scroll-locked");
      expect(document.body.style.overflow).toBe("auto");
    } finally {
      document.body.style.overflow = "";
    }
  });

  it("弹窗内展开 Radix Select 时，首次 Escape 仅关闭下拉列表，再次 Escape 才关闭弹窗", async () => {
    const user = userEvent.setup();

    function ModalWithSelectHarness() {
      const [open, setOpen] = useState(false);
      const [value, setValue] = useState("v1");
      return (
        <>
          <button onClick={() => setOpen(true)} type="button">
            打开选择弹窗
          </button>
          <Modal
            onClose={() => setOpen(false)}
            open={open}
            title="版本设置"
          >
            <Select onValueChange={setValue} value={value}>
              <SelectTrigger aria-label="目标版本">
                <SelectValue placeholder="请选择版本" />
              </SelectTrigger>
              <SelectContent aria-label="目标版本列表">
                <SelectItem value="v1">v1</SelectItem>
                <SelectItem value="v2">v2</SelectItem>
              </SelectContent>
            </Select>
          </Modal>
        </>
      );
    }

    render(<ModalWithSelectHarness />);
    const openButton = screen.getByRole("button", { name: "打开选择弹窗" });
    await user.click(openButton);
    const dialog = screen.getByRole("dialog", { name: "版本设置" });
    const combobox = within(dialog).getByRole("combobox", {
      name: "目标版本",
    });

    await user.click(combobox);
    expect(
      screen.getByRole("listbox", { name: "目标版本列表" }),
    ).toBeInTheDocument();

    // 首次 Escape 仅收起 Radix Select，外层 Modal 保持打开。
    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("listbox", { name: "目标版本列表" }),
    ).not.toBeInTheDocument();
    expect(dialog).toBeInTheDocument();

    // 再次 Escape 关闭 Modal 并归还焦点。
    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: "版本设置" }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(openButton).toHaveFocus());
  });

  it("未手动传 data-modal-escape-owner 时，展开态 combobox 触发器按 Escape 也不关闭外层弹窗", async () => {
    const user = userEvent.setup();

    function UnmarkedComboboxModalHarness() {
      const [open, setOpen] = useState(false);
      const [expanded, setExpanded] = useState(false);
      return (
        <>
          <button onClick={() => setOpen(true)} type="button">
            打开触发器弹窗
          </button>
          <Modal
            onClose={() => setOpen(false)}
            open={open}
            title="触发器兼容弹窗"
          >
            <button
              aria-expanded={expanded}
              aria-label="未加标记的下拉触发器"
              onClick={() => setExpanded((prev) => !prev)}
              onKeyDown={(event) => {
                if (event.key === "Escape" && expanded) {
                  setExpanded(false);
                }
              }}
              role="combobox"
              type="button"
            >
              选择项
            </button>
          </Modal>
        </>
      );
    }

    render(<UnmarkedComboboxModalHarness />);
    const openButton = screen.getByRole("button", { name: "打开触发器弹窗" });
    await user.click(openButton);
    const dialog = screen.getByRole("dialog", { name: "触发器兼容弹窗" });
    const combobox = within(dialog).getByRole("combobox", {
      name: "未加标记的下拉触发器",
    });

    await user.click(combobox);
    expect(combobox).toHaveAttribute("aria-expanded", "true");

    // 触发器处于展开态且未标记 data-modal-escape-owner，首次 Escape 仅收起触发器。
    await user.keyboard("{Escape}");
    expect(combobox).toHaveAttribute("aria-expanded", "false");
    expect(dialog).toBeInTheDocument();

    // 触发器收起后再次 Escape 才关闭外层 Modal。
    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: "触发器兼容弹窗" }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(openButton).toHaveFocus());
  });

  it("嵌套弹窗在同一 commit 中同时以 open={true} 挂载时，内层始终为栈顶且可经关闭按钮与 Escape 正常关闭", async () => {
    const user = userEvent.setup();

    function SimultaneousNestedModalHarness() {
      const [outerOpen, setOuterOpen] = useState(true);
      const [innerOpen, setInnerOpen] = useState(true);
      return (
        <Modal
          onClose={() => setOuterOpen(false)}
          open={outerOpen}
          title="同时挂载外层"
        >
          <p>外层正文</p>
          <Modal
            onClose={() => setInnerOpen(false)}
            open={innerOpen}
            title="同时挂载内层"
          >
            <input aria-label="内层聚焦框" data-autofocus />
          </Modal>
        </Modal>
      );
    }

    // 场景 1：同时以 open={true} 挂载后，通过关闭按钮关闭内层。
    const firstRender = render(<SimultaneousNestedModalHarness />);
    const firstInner = screen.getByRole("dialog", { name: "同时挂载内层" });
    const firstOuter = screen
      .getByText("同时挂载外层")
      .closest<HTMLElement>('[role="dialog"]');
    expect(firstOuter).not.toBeNull();
    expect(firstInner).toHaveAttribute("aria-modal", "true");
    expect(firstOuter).not.toHaveAttribute("aria-modal");

    await user.click(
      within(firstInner).getByRole("button", { name: "关闭弹窗" }),
    );
    expect(
      screen.queryByRole("dialog", { name: "同时挂载内层" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "同时挂载外层" }),
    ).toHaveAttribute("aria-modal", "true");
    firstRender.unmount();

    // 场景 2：同时以 open={true} 挂载后，首帧直接按 Escape 仅关闭内层且不死锁。
    render(<SimultaneousNestedModalHarness />);
    const secondInner = screen.getByRole("dialog", { name: "同时挂载内层" });
    const secondOuter = screen
      .getByText("同时挂载外层")
      .closest<HTMLElement>('[role="dialog"]');
    expect(secondInner).toHaveAttribute("aria-modal", "true");
    expect(secondOuter).not.toHaveAttribute("aria-modal");

    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: "同时挂载内层" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "同时挂载外层" }),
    ).toHaveAttribute("aria-modal", "true");
  });
});

