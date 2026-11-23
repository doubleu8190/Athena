import { QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { makeSkill } from "../../test/adminFixtures";
import { getRequestUrl } from "../../test/fixtures";
import {
  createTestQueryClient,
  jsonResponse,
} from "../../test/testUtils";
import { SkillCatalogPicker } from "./SkillCatalogPicker";

const COMPANY_LEGACY_SKILL = makeSkill({
  current_version_label: "v3",
  description: "按统一模板生成项目复盘。",
  id: "company-review",
  name: "项目复盘",
  // 故意保留历史 "public" 枚举，验证前端将其安全归一到「全公司技能」。
  scope: "public",
  script_requires_approval: true,
  slug: "project-review",
});

const DEPARTMENT_SKILL = makeSkill({
  current_version_label: "v1",
  description: "按财务部门规范校验费用报销单。",
  id: "dept-expense",
  name: "费用报销审核",
  owner_department_id: "dept-fin",
  scope: "department",
  script_requires_approval: false,
  slug: "expense-audit",
});

const PERSONAL_SKILL = makeSkill({
  current_version_label: "v2",
  description: "只对本人可见的会议纪要方法。",
  enabled: true,
  id: "personal-notes",
  name: "会议纪要",
  scope: "personal",
  script_requires_approval: false,
  slug: "meeting-notes",
});

function PickerHarness({
  disabled = false,
  initialSelected = [],
}: {
  disabled?: boolean;
  initialSelected?: string[];
}) {
  const [selected, setSelected] = useState(initialSelected);
  return (
    <>
      <SkillCatalogPicker
        disabled={disabled}
        disabledNote="任务运行中，下一轮可调整。"
        onChange={setSelected}
        selectedSkillIds={selected}
      />
      <output data-testid="selected-skills">{selected.join(",")}</output>
      <button type="button">外部按钮</button>
    </>
  );
}

function renderPicker(initialSelected: string[] = []) {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <PickerHarness initialSelected={initialSelected} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Composer 技能选择器", () => {
  it("默认自动选择，展开后按全公司/部门共享/个人三层分组，支持搜索并完成选择", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        expect(url.pathname).toBe("/api/v1/skills");
        return jsonResponse({
          items: [COMPANY_LEGACY_SKILL, DEPARTMENT_SKILL, PERSONAL_SKILL],
        });
      }),
    );
    const user = userEvent.setup();
    renderPicker();

    const trigger = await screen.findByRole("button", {
      name: /本次技能/,
    });
    expect(trigger).toHaveTextContent("本次技能");
    expect(trigger).toHaveTextContent("自动选择");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(trigger);
    const dialog = screen.getByRole("dialog", {
      name: "选择本次技能",
    });
    expect(
      within(dialog).getByRole("heading", { name: "全公司技能" }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("heading", { name: "部门共享技能" }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("heading", { name: "我的技能" }),
    ).toBeInTheDocument();

    const companyOption = within(dialog)
      .getByText("项目复盘")
      .closest("label");
    const deptOption = within(dialog)
      .getByText("费用报销审核")
      .closest("label");
    const personalOption = within(dialog)
      .getByText("会议纪要")
      .closest("label");
    expect(companyOption).not.toBeNull();
    expect(deptOption).not.toBeNull();
    expect(personalOption).not.toBeNull();
    expect(
      within(companyOption as HTMLElement).getByText("全公司"),
    ).toBeVisible();
    expect(
      within(companyOption as HTMLElement).getByText("脚本执行需审批"),
    ).toBeVisible();
    expect(within(companyOption as HTMLElement).getByText("v3")).toBeVisible();
    expect(
      within(deptOption as HTMLElement).getByText("部门共享"),
    ).toBeVisible();
    expect(
      within(personalOption as HTMLElement).getByText("个人"),
    ).toBeVisible();
    expect(
      within(personalOption as HTMLElement).getByText("纯文档"),
    ).toBeVisible();

    // 关键字过滤只展示命中项
    const searchInput = within(dialog).getByLabelText("搜索技能");
    await user.type(searchInput, "报销");
    expect(within(dialog).getByText("费用报销审核")).toBeInTheDocument();
    expect(within(dialog).queryByText("项目复盘")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("会议纪要")).not.toBeInTheDocument();

    await user.clear(searchInput);
    await user.click(
      within(dialog).getByRole("checkbox", { name: /项目复盘/ }),
    );
    expect(screen.getByTestId("selected-skills")).toHaveTextContent(
      COMPANY_LEGACY_SKILL.id,
    );
    await user.click(within(dialog).getByRole("button", { name: "完成" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(trigger).toHaveTextContent("项目复盘");
  });

  it("Escape 关闭时返还触发器焦点，点击外部保留外部焦点", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ items: [COMPANY_LEGACY_SKILL] })),
    );
    const user = userEvent.setup();
    renderPicker();

    const trigger = await screen.findByRole("button", {
      name: /本次技能/,
    });
    await user.click(trigger);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());

    await user.click(trigger);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "外部按钮" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "外部按钮" })).toHaveFocus();
  });

  it("关闭按钮会把焦点还给技能触发按钮", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ items: [COMPANY_LEGACY_SKILL] })),
    );
    const user = userEvent.setup();
    renderPicker();

    const trigger = await screen.findByRole("button", {
      name: /本次技能/,
    });
    await user.click(trigger);
    const dialog = screen.getByRole("dialog", {
      name: "选择本次技能",
    });
    await user.click(
      within(dialog).getByRole("button", {
        name: "关闭技能选择面板",
      }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("不可用已选项不会被静默替换，可显式移除", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ items: [COMPANY_LEGACY_SKILL] })),
    );
    const user = userEvent.setup();
    renderPicker(["missing-skill", COMPANY_LEGACY_SKILL.id]);

    const trigger = await screen.findByRole("button", {
      name: /本次技能/,
    });
    await waitFor(() => expect(trigger).toHaveTextContent("含不可用项"));
    await user.click(trigger);
    const dialog = screen.getByRole("dialog", {
      name: "选择本次技能",
    });
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "页面不会自动替换",
    );
    expect(screen.getByTestId("selected-skills")).toHaveTextContent(
      "missing-skill",
    );

    await user.click(
      within(dialog).getByRole("button", {
        name: "移除不可用项",
      }),
    );
    expect(screen.getByTestId("selected-skills")).toHaveTextContent(
      COMPANY_LEGACY_SKILL.id,
    );
    expect(screen.getByTestId("selected-skills")).not.toHaveTextContent(
      "missing-skill",
    );
    expect(within(dialog).getByRole("button", { name: "完成" })).toHaveFocus();
  });

  it("移除全部不可用项后仍把焦点留在稳定的完成按钮", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ items: [COMPANY_LEGACY_SKILL] })),
    );
    const user = userEvent.setup();
    renderPicker(["missing-skill"]);

    await user.click(await screen.findByRole("button", { name: /本次技能/ }));
    const dialog = screen.getByRole("dialog", {
      name: "选择本次技能",
    });
    const removeUnavailable = within(dialog).getByRole("button", {
      name: "移除不可用项",
    });
    removeUnavailable.focus();
    await user.keyboard("{Enter}");

    expect(screen.getByTestId("selected-skills")).toHaveTextContent("");
    expect(within(dialog).getByRole("button", { name: "完成" })).toHaveFocus();
    expect(
      within(dialog).queryByRole("button", { name: "移除不可用项" }),
    ).not.toBeInTheDocument();
  });

  it("复选框聚焦时 Enter 不提交外层表单，Space 仍切换勾选", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ items: [COMPANY_LEGACY_SKILL] })),
    );
    const user = userEvent.setup();
    const handleSubmit = vi.fn((event: React.FormEvent) => {
      event.preventDefault();
    });
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <form onSubmit={handleSubmit}>
          <textarea aria-label="对话草稿" defaultValue="请复盘这个项目" />
          <PickerHarness />
          <button type="submit">发送</button>
        </form>
      </QueryClientProvider>,
    );

    await user.click(await screen.findByRole("button", { name: /本次技能/ }));
    const dialog = screen.getByRole("dialog", {
      name: "选择本次技能",
    });
    await waitFor(() =>
      expect(dialog.contains(document.activeElement)).toBe(true),
    );

    const checkbox = within(dialog).getByRole("checkbox", {
      name: /项目复盘/,
    });
    checkbox.focus();
    expect(checkbox).toHaveFocus();

    expect(fireEvent.keyDown(checkbox, { key: "Enter" })).toBe(false);
    expect(handleSubmit).not.toHaveBeenCalled();
    expect(checkbox).not.toBeChecked();

    await user.keyboard(" ");
    expect(checkbox).toBeChecked();
    expect(screen.getByTestId("selected-skills")).toHaveTextContent(
      COMPANY_LEGACY_SKILL.id,
    );
  });

  it("键盘恢复自动选择后焦点留在完成按钮且恢复按钮禁用", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ items: [COMPANY_LEGACY_SKILL] })),
    );
    const user = userEvent.setup();
    renderPicker([COMPANY_LEGACY_SKILL.id]);

    await user.click(await screen.findByRole("button", { name: /本次技能/ }));
    const dialog = screen.getByRole("dialog", {
      name: "选择本次技能",
    });
    await waitFor(() =>
      expect(dialog.contains(document.activeElement)).toBe(true),
    );

    const restoreAutomatic = within(dialog).getByRole("button", {
      name: "恢复自动选择",
    });
    restoreAutomatic.focus();
    await user.keyboard("{Enter}");

    const completeButton = within(dialog).getByRole("button", {
      name: "完成",
    });
    expect(screen.getByTestId("selected-skills")).toHaveTextContent("");
    expect(restoreAutomatic).toBeDisabled();
    expect(completeButton).toBeEnabled();
    expect(completeButton).toHaveFocus();
  });

  it("弹层展示加载与当前无可用技能状态，运行中禁用并说明下一轮可调整", async () => {
    let resolveSkills: ((value: Response) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveSkills = resolve;
          }),
      ),
    );
    const user = userEvent.setup();
    const app = renderPicker();

    const trigger = await screen.findByRole("button", {
      name: /本次技能/,
    });
    await user.click(trigger);
    expect(screen.getByText("正在读取技能库…")).toBeInTheDocument();
    resolveSkills?.(jsonResponse({ items: [] }));
    expect(await screen.findByText("当前没有可用技能")).toBeInTheDocument();
    app.unmount();

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ items: [] })),
    );
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <PickerHarness disabled />
      </QueryClientProvider>,
    );
    const disabledTrigger = screen.getByRole("button", {
      name: /本次技能/,
    });
    expect(disabledTrigger).toBeDisabled();
    expect(
      screen.getByText("任务运行中，下一轮可调整。"),
    ).toBeInTheDocument();
  });

  it("技能库加载失败时在弹层内显示错误并可重试", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({ detail: "skill_catalog_unavailable" }, 500),
      ),
    );
    const user = userEvent.setup();
    renderPicker();

    await user.click(await screen.findByRole("button", { name: /本次技能/ }));
    const dialog = screen.getByRole("dialog", {
      name: "选择本次技能",
    });
    expect(within(dialog).getByRole("alert")).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "重试" }),
    ).toBeInTheDocument();
  });
});
