import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { WorkspaceFilePicker } from "./WorkspaceFilePicker";

function FilePickerHarness() {
  const [file, setFile] = useState<File | null>(null);

  return (
    <div>
      <WorkspaceFilePicker
        describedBy="file-hint"
        file={file}
        id="test-file"
        label="测试文件"
        onChange={setFile}
        placeholder="未选择文件"
        triggerLabel="选择文件"
      />
      <p id="file-hint">支持 ZIP 文件。</p>
    </div>
  );
}

describe("WorkspaceFilePicker", () => {
  it("保留原生文件输入的无障碍关联，并支持选择与清除", async () => {
    const user = userEvent.setup();
    render(<FilePickerHarness />);

    const input = screen.getByLabelText("测试文件");
    expect(input).toHaveAttribute("aria-describedby", "file-hint");
    expect(screen.getByText("未选择文件")).toBeInTheDocument();

    const file = new File(["zip-content"], "skill-package.zip", {
      type: "application/zip",
    });
    await user.upload(input, file);
    expect(
      screen.getByText("skill-package.zip (11 B)"),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "清除已选文件" }),
    );
    expect(screen.getByText("未选择文件")).toBeInTheDocument();
    expect((input as HTMLInputElement).files).toHaveLength(0);
  });

  it("buttonTrigger 模式提供按钮触发器并将隐藏 input 移出 Tab 焦点序", async () => {
    const user = userEvent.setup();

    function ButtonTriggerHarness() {
      const [file, setFile] = useState<File | null>(null);
      return (
        <div>
          <label htmlFor="button-trigger-file">版本包</label>
          <WorkspaceFilePicker
            accept=".zip"
            buttonTrigger
            file={file}
            id="button-trigger-file"
            label="版本包"
            onChange={setFile}
            placeholder="未选择 .zip 文件"
            triggerLabel="选择 ZIP 文件"
          />
        </div>
      );
    }

    render(<ButtonTriggerHarness />);

    const input = screen.getByLabelText("版本包");
    expect(input).toHaveAttribute("tabindex", "-1");
    expect(
      screen.getByRole("button", { name: "选择 ZIP 文件" }),
    ).toBeInTheDocument();

    const file = new File(["abc"], "v2.zip", { type: "application/zip" });
    await user.upload(input, file);
    expect(screen.getByText("v2.zip (3 B)")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "清除已选文件" }));
    expect(screen.getByText("未选择 .zip 文件")).toBeInTheDocument();
  });
});
