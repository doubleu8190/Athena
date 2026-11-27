import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TEST_ADMIN } from "../../test/adminFixtures";
import { getRequestUrl, makeDeptAdmin } from "../../test/fixtures";
import { createTestQueryClient, jsonResponse } from "../../test/testUtils";
import type { OrgDepartment } from "../admin/model";
import { AuthProvider } from "../auth/AuthProvider";
import type { User } from "../auth/model";
import type { VisibilityGrantInput } from "../knowledge/model";
import {
  VisibilityGrantsEditor,
  defaultOwnerGrant,
} from "./VisibilityGrantsEditor";

const ACTIVE_DEPARTMENTS: OrgDepartment[] = [
  {
    created_at: "2026-10-01T00:00:00Z",
    external_id: "ext-fin",
    id: "dept-fin",
    name: "财务部",
    order_index: 1,
    parent_id: null,
    path: "/dept-fin",
    status: "active",
    synced_at: null,
    updated_at: "2026-10-01T00:00:00Z",
  },
  {
    created_at: "2026-10-01T00:00:00Z",
    external_id: "ext-ops",
    id: "dept-ops",
    name: "运营部",
    order_index: 2,
    parent_id: null,
    path: "/dept-ops",
    status: "active",
    synced_at: null,
    updated_at: "2026-10-01T00:00:00Z",
  },
  {
    created_at: "2026-10-01T00:00:00Z",
    external_id: "ext-hr",
    id: "dept-hr",
    name: "人力部",
    order_index: 3,
    parent_id: null,
    path: "/dept-hr",
    status: "active",
    synced_at: null,
    updated_at: "2026-10-01T00:00:00Z",
  },
];

function stubAuthAndDepartments(
  user: User,
  options: { departmentsError?: boolean } = {},
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      if (url.pathname.endsWith("/auth/me")) {
        return jsonResponse(user);
      }
      if (url.pathname === "/api/v1/admin/org/departments") {
        if (options.departmentsError) {
          return jsonResponse({ detail: "service_unavailable" }, 503);
        }
        return jsonResponse({ items: ACTIVE_DEPARTMENTS });
      }
      throw new Error(`Unexpected request: ${url}`);
    }),
  );
}

function Harness({
  departments,
  departmentsError,
  initialGrants,
  initialOwnerId,
  onOuterSubmit,
}: {
  departments?: OrgDepartment[];
  departmentsError?: boolean;
  initialGrants: VisibilityGrantInput[];
  initialOwnerId: string;
  onOuterSubmit?: () => void;
}) {
  const [ownerId, setOwnerId] = useState(initialOwnerId);
  const [grants, setGrants] = useState<VisibilityGrantInput[]>(initialGrants);

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onOuterSubmit?.();
      }}
    >
      <label htmlFor="owner-switcher">切换归属部门</label>
      <select
        id="owner-switcher"
        onChange={(event) => setOwnerId(event.target.value)}
        value={ownerId}
      >
        <option value="dept-fin">财务部</option>
        <option value="dept-ops">运营部</option>
        <option value="dept-hr">人力部</option>
      </select>

      <VisibilityGrantsEditor
        departments={departments}
        departmentsError={departmentsError}
        onChange={setGrants}
        ownerDepartmentId={ownerId}
        resourceLabel="知识库"
        value={grants}
      />

      <output data-testid="grants-json">{JSON.stringify(grants)}</output>
      <button type="submit">提交外层表单</button>
    </form>
  );
}

function renderWithAuth(ui: JSX.Element) {
  const client = createTestQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <AuthProvider>{ui}</AuthProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("VisibilityGrantsEditor", () => {
  it("切换 ownerDepartmentId 时清除旧归属部门、保留手工追加项且自动去重同步回父级", async () => {
    stubAuthAndDepartments(TEST_ADMIN);
    const user = userEvent.setup();

    renderWithAuth(
      <Harness
        departments={ACTIVE_DEPARTMENTS}
        initialGrants={[
          defaultOwnerGrant("dept-fin", true),
          {
            include_subdepartments: true,
            target_id: "dept-ops",
            target_type: "department",
          },
          {
            include_subdepartments: false,
            target_id: "member",
            target_type: "role",
          },
        ]}
        initialOwnerId="dept-fin"
      />,
    );

    const editor = await screen.findByTestId("visibility-grants-editor");
    expect(within(editor).getByText("共 3 条授权")).toBeInTheDocument();

    // 切换归属部门为原本已作为附加部门存在的 dept-ops：
    // 旧归属 dept-fin 应被移除，dept-ops 去重晋升为归属行，附加角色 member 保留。
    await user.selectOptions(
      screen.getByLabelText("切换归属部门"),
      "dept-ops",
    );

    await waitFor(() => {
      const synced = JSON.parse(
        screen.getByTestId("grants-json").textContent ?? "[]",
      ) as VisibilityGrantInput[];
      expect(synced).toEqual([
        {
          include_subdepartments: true,
          target_id: "dept-ops",
          target_type: "department",
        },
        {
          include_subdepartments: false,
          target_id: "member",
          target_type: "role",
        },
      ]);
    });
    expect(within(editor).getByText("共 2 条授权")).toBeInTheDocument();
  });

  it("目录加载失败降级手工输入：校验 64 字符上限与控制字符、拦截 Enter 防误提外层表单并自动去重", async () => {
    stubAuthAndDepartments(TEST_ADMIN, { departmentsError: true });
    const onOuterSubmit = vi.fn();
    const user = userEvent.setup();

    renderWithAuth(
      <Harness
        departments={[]}
        departmentsError
        initialGrants={[defaultOwnerGrant("dept-fin", true)]}
        initialOwnerId="dept-fin"
        onOuterSubmit={onOuterSubmit}
      />,
    );

    const manualInput = await screen.findByLabelText("输入附加共享部门 ID");
    expect(manualInput).toHaveAttribute("maxlength", "64");

    // 1. 超过 64 字符拒绝添加并展示 role="alert"
    fireEvent.change(manualInput, { target: { value: "d".repeat(65) } });
    await user.click(screen.getByRole("button", { name: "添加共享" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/不能超过 64 个字符/);
    expect(manualInput).toHaveAttribute("aria-invalid", "true");

    // 2. 包含控制字符拒绝添加
    fireEvent.change(manualInput, { target: { value: "dept-\u0007bad" } });
    await user.click(screen.getByRole("button", { name: "添加共享" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/不能包含控制字符/);

    // 3. 输入合法部门 ID 后按 Enter：仅添加共享条目，绝不触发外层表单 submit
    fireEvent.change(manualInput, { target: { value: "dept-manual-1" } });
    fireEvent.keyDown(manualInput, { key: "Enter" });
    expect(onOuterSubmit).not.toHaveBeenCalled();

    const syncedAfterEnter = JSON.parse(
      screen.getByTestId("grants-json").textContent ?? "[]",
    ) as VisibilityGrantInput[];
    expect(syncedAfterEnter).toEqual([
      {
        include_subdepartments: true,
        target_id: "dept-fin",
        target_type: "department",
      },
      {
        include_subdepartments: true,
        target_id: "dept-manual-1",
        target_type: "department",
      },
    ]);

    // 4. 重复输入同一部门 ID 自动去重并提示
    const editor = screen.getByTestId("visibility-grants-editor");
    fireEvent.change(manualInput, { target: { value: "dept-manual-1" } });
    await user.click(screen.getByRole("button", { name: "添加共享" }));
    expect(within(editor).getByRole("status")).toHaveTextContent(/已自动去重/);
  });

  it("dept_admin 权限裁剪：归属部门与新增附加部门默认不含子部门，绝不渲染角色授权且仅展示本人管辖部门", async () => {
    const deptAdmin = makeDeptAdmin({
      departments: [
        { id: "dept-fin", is_primary: true, name: "财务部", path: "/dept-fin" },
        {
          id: "dept-audit",
          is_primary: false,
          name: "内审部",
          path: "/dept-audit",
        },
      ],
      managed_department_ids: ["dept-fin", "dept-audit"],
    });
    stubAuthAndDepartments(deptAdmin);
    const user = userEvent.setup();

    renderWithAuth(
      <Harness
        initialGrants={[]}
        initialOwnerId="dept-fin"
      />,
    );

    const editor = await screen.findByTestId("visibility-grants-editor");
    const ownerCheckbox = within(editor).getByRole("checkbox", {
      name: "包含下级子部门",
    });
    await waitFor(() => expect(ownerCheckbox).not.toBeChecked());

    expect(
      within(editor).queryByLabelText("附加授权类型"),
    ).not.toBeInTheDocument();
    expect(
      within(editor).queryByLabelText("选择附加共享角色"),
    ).not.toBeInTheDocument();

    const extraDeptSelect = within(editor).getByLabelText("选择附加共享部门");
    expect(within(extraDeptSelect).getByText("内审部")).toBeInTheDocument();
    expect(
      within(extraDeptSelect).queryByText("运营部"),
    ).not.toBeInTheDocument();

    await user.selectOptions(extraDeptSelect, "dept-audit");
    await user.click(within(editor).getByRole("button", { name: "添加共享" }));

    const synced = JSON.parse(
      screen.getByTestId("grants-json").textContent ?? "[]",
    ) as VisibilityGrantInput[];
    expect(synced).toEqual([
      {
        include_subdepartments: false,
        target_id: "dept-fin",
        target_type: "department",
      },
      {
        include_subdepartments: false,
        target_id: "dept-audit",
        target_type: "department",
      },
    ]);
  });
});
