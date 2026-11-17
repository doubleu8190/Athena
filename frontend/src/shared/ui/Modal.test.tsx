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

    await user.click(
      within(outer).getByRole("button", { name: "关闭弹窗" }),
    );
    expect(inner).toBeInTheDocument();
    expect(outer).toBeInTheDocument();

    fireEvent.mouseDown(outer.parentElement as HTMLElement);
    expect(inner).toBeInTheDocument();
    expect(outer).toBeInTheDocument();

    fireEvent.mouseDown(inner.parentElement as HTMLElement);
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
    await user.click(
      within(outer).getByRole("button", { name: "关闭弹窗" }),
    );
    fireEvent.mouseDown(outer.parentElement as HTMLElement);
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
      within(outer).getByRole("button", { name: "打开内层弹窗" }),
    ).not.toHaveFocus();
  });

  it("焦点循环跳过程序化和禁用控件", async () => {
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
    const fieldsetDisabledButton = screen.getByRole("button", {
      name: "字段集禁用按钮",
    });
    const firstControl = screen.getByRole("button", {
      name: "关闭弹窗",
    });
    await waitFor(() => expect(lastVisibleControl).toHaveFocus());

    fireEvent.keyDown(lastVisibleControl, { key: "Tab" });
    expect(firstControl).toHaveFocus();
    expect(hiddenInput).not.toHaveFocus();
    expect(programmaticButton).not.toHaveFocus();
    expect(fieldsetDisabledButton).not.toHaveFocus();
  });
});

