import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { SelectDropdown, type SelectDropdownOption } from "./SelectDropdown";

function DropdownHarness({
  disabled = false,
  initialValue = "",
  options,
}: {
  disabled?: boolean;
  initialValue?: string;
  options: readonly SelectDropdownOption[];
}) {
  const [value, setValue] = useState(initialValue);
  return (
    <div>
      <SelectDropdown
        classPrefix="test-dropdown"
        disabled={disabled}
        id="test-select"
        label="选择目标"
        listboxLabel="目标列表"
        onChange={setValue}
        options={options}
        placeholder="请选择"
        value={value}
      />
      <button type="button">外部按钮</button>
    </div>
  );
}

describe("SelectDropdown", () => {
  const OPTIONS: SelectDropdownOption[] = [
    { label: "未归属", value: "" },
    { label: "项目甲", value: "a" },
    { badge: "不可用", disabled: true, label: "已禁用项", value: "disabled" },
    { badge: "依赖未满足", label: "项目乙", value: "b" },
  ];

  it("支持键盘导航、空字符串值提交、跳过禁用项与 Escape 消费标记", async () => {
    const user = userEvent.setup();
    render(<DropdownHarness initialValue="a" options={OPTIONS} />);

    const trigger = screen.getByRole("combobox", { name: "选择目标" });
    expect(trigger).toHaveTextContent("项目甲");
    expect(trigger).not.toHaveAttribute("data-modal-escape-owner");

    trigger.focus();
    await user.keyboard("{ArrowDown}");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(trigger).toHaveAttribute("data-modal-escape-owner", "self");
    expect(
      screen.getByRole("listbox", { name: "目标列表" }),
    ).toBeInTheDocument();

    // 向下移动自动跳过 disabled 项落到“项目乙”
    await user.keyboard("{ArrowDown}");
    const optionB = screen.getByRole("option", { name: /项目乙/ });
    expect(optionB).toHaveAttribute("data-active", "true");
    expect(optionB).toHaveTextContent("依赖未满足");

    // Home 回到空字符串值选项并可用 Enter 正常提交
    await user.keyboard("{Home}");
    expect(
      screen.getByRole("option", { name: /未归属/ }),
    ).toHaveAttribute("data-active", "true");
    await user.keyboard("{Enter}");
    expect(trigger).toHaveTextContent("未归属");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).not.toHaveAttribute("data-modal-escape-owner");

    // End + Space 选中最后一项
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{End}");
    await user.keyboard(" ");
    expect(trigger).toHaveTextContent("项目乙");

    // 点击外部关闭菜单
    await user.click(trigger);
    expect(
      screen.getByRole("listbox", { name: "目标列表" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "外部按钮" }));
    expect(
      screen.queryByRole("listbox", { name: "目标列表" }),
    ).not.toBeInTheDocument();
  });

  it("禁用态不展开菜单且 Escape 关闭已展开菜单", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <DropdownHarness initialValue="a" options={OPTIONS} />,
    );

    const trigger = screen.getByRole("combobox", { name: "选择目标" });
    await user.click(trigger);
    expect(
      screen.getByRole("listbox", { name: "目标列表" }),
    ).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("listbox", { name: "目标列表" }),
    ).not.toBeInTheDocument();

    await user.click(trigger);
    rerender(<DropdownHarness disabled initialValue="a" options={OPTIONS} />);
    expect(trigger).toBeDisabled();
    expect(
      screen.queryByRole("listbox", { name: "目标列表" }),
    ).not.toBeInTheDocument();
  });

  it("首尾存在禁用项时，边界继续按 ArrowDown / ArrowUp 原地停留而不跳回已选项", async () => {
    const user = userEvent.setup();
    const edgeDisabledOptions: SelectDropdownOption[] = [
      { disabled: true, label: "顶部禁用项", value: "top-disabled" },
      { label: "首可用项 A", value: "a" },
      { label: "末可用项 B", value: "b" },
      { disabled: true, label: "尾部禁用项", value: "bottom-disabled" },
    ];

    render(
      <DropdownHarness initialValue="a" options={edgeDisabledOptions} />,
    );
    const trigger = screen.getByRole("combobox", { name: "选择目标" });
    await user.click(trigger);

    // 向下移到最后一个可用项 B，再继续按 ArrowDown 应停留在 B，而不是回跳到已选项 A。
    await user.keyboard("{ArrowDown}");
    const optionB = screen.getByRole("option", { name: "末可用项 B" });
    expect(optionB).toHaveAttribute("data-active", "true");
    await user.keyboard("{ArrowDown}");
    expect(optionB).toHaveAttribute("data-active", "true");

    // 选中 B 后重新展开并向上移到第一个可用项 A，再继续按 ArrowUp 应停留在 A，不回跳到已选项 B。
    await user.keyboard("{Enter}");
    expect(trigger).toHaveTextContent("末可用项 B");

    await user.click(trigger);
    await user.keyboard("{ArrowUp}");
    const optionA = screen.getByRole("option", { name: "首可用项 A" });
    expect(optionA).toHaveAttribute("data-active", "true");
    await user.keyboard("{ArrowUp}");
    expect(optionA).toHaveAttribute("data-active", "true");
  });
});
