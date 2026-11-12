import { expect, test, type Page, type TestInfo } from "@playwright/test";

import {
  apiJSON,
  attachSanitizedManifest,
  attachScreenshot,
  loadSkillsLiveState,
  loginThroughUi,
  saveDownloadEvidence,
  waitForSkillScriptCleanup,
  waitForSkillScriptStart,
  writeInvalidZip,
  type LiveScenario,
  type SkillsLiveState,
} from "./skills.live.fixtures";

interface SkillSummary {
  current_version_label: string | null;
  enabled: boolean;
  id: string;
  script_requires_approval: boolean;
  slug: string;
}

interface SkillListResponse {
  items: SkillSummary[];
}

interface Run {
  id: string;
  input_file_ids: string[];
  pending_approvals: Array<{ call_id: string; model_name: string }>;
  skill_snapshot: {
    selected_skill_ids: string[];
    skills: Array<{ skill_id: string; version_label: string }>;
  } | null;
  safe_error_code: string | null;
  status: string;
}

interface RunListResponse {
  items: Run[];
}

interface ToolCallListResponse {
  items: Array<{
    id: string;
    model_name: string;
    status: string;
    tool_call_id: string;
    tool_id: string | null;
  }>;
}

interface ToolCallDetail {
  artifacts: Array<{
    file_id: string;
    filename: string;
    label: string | null;
  }>;
  arguments: { file_id?: string } | null;
  safe_skill_projection: {
    input_files: Array<{ filename: string }>;
    skill_name: string;
    skill_version_label: string;
  } | null;
  status: string;
  outcome: {
    data: unknown;
    status: string;
  } | null;
}

interface SessionFileListResponse {
  items: Array<{
    filename: string;
    id: string;
    label: string | null;
  }>;
}

interface ConversationMessagePage {
  items: Array<{
    id: string;
    parts: unknown[];
    role: string;
    run_id: string | null;
    sequence: number;
    status: string;
  }>;
  next_before_sequence: number | null;
}

interface RunEventPage {
  items: Array<{
    id: string;
    payload: Record<string, unknown>;
    run_id: string;
    sequence: number;
    type: string;
  }>;
  next_after_sequence: number | null;
}

const STATUS_LABELS: Record<string, string> = {
  cancelled: "已取消",
  completed: "已完成",
  failed: "失败",
};

function expectedArtifactsForVersion(
  baseNames: string[],
  versionLabel: string,
): string[] {
  const match = /(?:^|[^0-9])v?(\d+)$/i.exec(versionLabel.trim());
  if (!match) {
    throw new Error(
      "无法从固定版本标签推导产物序号：" + versionLabel,
    );
  }
  const version = Number(match[1]);
  const suffix = version === 1 ? "" : "-v" + String(version);
  return baseNames.map((name) =>
    name.replace(/(\.[^.]+)$/, suffix + "$1"),
  );
}

async function currentSkills(page: Page): Promise<SkillListResponse> {
  const result = await apiJSON<SkillListResponse>(
    page,
    "/api/v1/admin/skills",
  );
  expect(result.status).toBe(200);
  return result.body;
}

async function publicSkills(page: Page): Promise<SkillListResponse> {
  const result = await apiJSON<SkillListResponse>(
    page,
    "/api/v1/skills",
  );
  expect(result.status).toBe(200);
  return result.body;
}

async function latestRun(page: Page, sessionId: string): Promise<Run> {
  const result = await apiJSON<RunListResponse>(
    page,
    "/api/v1/sessions/" +
      encodeURIComponent(sessionId) +
      "/runs?limit=1",
  );
  expect(result.status).toBe(200);
  expect(result.body.items.length).toBeGreaterThan(0);
  return result.body.items[0];
}

async function waitForRunStatus(
  page: Page,
  sessionId: string,
  expectedStatus: string,
  previousRunId: string | null = null,
): Promise<Run> {
  await expect
    .poll(
      async () => {
        const run = await latestRun(page, sessionId);
        return run.id !== previousRunId ? run.status : "waiting_new_run";
      },
      { timeout: 120_000 },
    )
    .toBe(expectedStatus);
  const run = await latestRun(page, sessionId);
  await expect(
    page.locator(".run-summary .run-status").first(),
  ).toHaveText(STATUS_LABELS[expectedStatus] ?? expectedStatus);
  return run;
}

async function openImportedSkill(page: Page, state: SkillsLiveState) {
  const card = page
    .locator(".admin-skill-card")
    .filter({ hasText: state.skillName })
    .first();
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: "查看版本与指南" }).click();
  const dialog = page.getByRole("dialog", {
    name: "技能：" + state.skillName,
  });
  await expect(dialog).toBeVisible();
  return dialog;
}

async function uploadVersionTwo(
  page: Page,
  state: SkillsLiveState,
): Promise<string> {
  const dialog = await openImportedSkill(page, state);
  await dialog
    .getByLabel("选择新版本 ZIP")
    .setInputFiles(state.versionTwoZipPath);
  const label = "browser-live-version-2";
  await dialog.getByLabel("版本标签（可选）").fill(label);
  await dialog.getByRole("button", { name: "上传新版本" }).click();
  await expect(
    dialog.getByText(/已保存。它尚未自动成为当前版本/),
  ).toBeVisible({ timeout: 60_000 });
  const versionTab = dialog
    .locator(".admin-skill-version-tab")
    .filter({ hasText: label })
    .first();
  await expect(versionTab).toBeVisible();
  await versionTab.click();
  await dialog
    .getByRole("button", { name: "保存当前配置" })
    .click();
  await expect(dialog.getByText(/技能配置已保存/)).toBeVisible();
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  return label;
}

function pageApprovalVersion(
  page: Page,
  state: SkillsLiveState,
  versionLabel: string,
) {
  return page
    .locator(".approval-row")
    .filter({ hasText: state.skillName })
    .getByText(versionLabel, { exact: true });
}

async function setSkillEnabled(
  page: Page,
  state: SkillsLiveState,
  enabled: boolean,
): Promise<void> {
  const dialog = await openImportedSkill(page, state);
  const toggle = dialog.getByRole("checkbox", {
    name: "向普通用户启用该技能",
  });
  await toggle.setChecked(enabled);
  await dialog
    .getByRole("button", { name: "保存当前配置" })
    .click();
  await expect(dialog.getByText(/技能配置已保存/)).toBeVisible();
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
}

async function startSkillRun(
  page: Page,
  state: SkillsLiveState,
  prompt: string,
  selectSkill = true,
): Promise<string> {
  await page.goto("/chat");
  const skillOption = page
    .locator("label.skill-catalog-option")
    .filter({ hasText: state.skillName })
    .first();
  await expect(skillOption).toBeVisible();
  if (selectSkill) {
    await skillOption.getByRole("checkbox").check();
  }
  await page.getByLabel("选择任务文件").setInputFiles(state.csvPath);
  await expect(
    page.getByText(state.csvFilename, { exact: true }).first(),
  ).toBeVisible();
  await page.getByLabel("对话草稿").fill(prompt);
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page).toHaveURL(/\/chat\/[^/]+$/);
  const sessionId = new URL(page.url()).pathname.split("/").at(-1);
  expect(sessionId).toBeTruthy();
  return sessionId as string;
}

async function waitForApproval(
  page: Page,
  sessionId: string,
  state: SkillsLiveState,
  versionLabel: string,
) {
  const approval = page
    .locator(".approval-row")
    .filter({ hasText: state.skillName })
    .first();
  await expect
    .poll(
      async () => {
        const run = await latestRun(page, sessionId);
        if (["completed", "failed", "cancelled"].includes(run.status)) {
          throw new Error(
            "Run 在审批前进入 " +
              run.status +
              "：" +
              (run.safe_error_code ?? "no_safe_error_code"),
          );
        }
        return (await approval.isVisible()) ? "approval" : run.status;
      },
      { timeout: 90_000 },
    )
    .toBe("approval");
  await expect(
    page.getByRole("heading", { name: "等待工具审批" }),
  ).toBeVisible();
  await expect(approval).toBeVisible();
  await expect(approval.getByText(state.skillName).first()).toBeVisible();
  await expect(
    approval.getByText(versionLabel, { exact: true }),
  ).toBeVisible();
  await expect(
    approval.getByText(state.csvFilename, { exact: false }),
  ).toBeVisible();
  await approval
    .getByRole("button", { name: "查看安全详情" })
    .click();
  await expect(approval.getByText("可展示参数")).toBeVisible();
  return approval;
}

async function waitForExecutingSkillCall(
  page: Page,
  sessionId: string,
  runId: string,
) {
  let executingCall: ToolCallListResponse["items"][number] | null = null;
  await expect
    .poll(
      async () => {
        const result = await apiJSON<ToolCallListResponse>(
          page,
          "/api/v1/sessions/" +
            encodeURIComponent(sessionId) +
            "/runs/" +
            encodeURIComponent(runId) +
            "/tool-calls?limit=100",
        );
        expect(result.status).toBe(200);
        executingCall =
          result.body.items.find(
            (call) =>
              call.tool_id === "builtin_skill_execute" ||
              call.model_name === "skill_execute" ||
              call.model_name.endsWith("_skill_execute"),
          ) ?? null;
        return executingCall?.status ?? "missing";
      },
      { timeout: 60_000 },
    )
    .toBe("executing");
  expect(executingCall).not.toBeNull();
  return executingCall as ToolCallListResponse["items"][number];
}

async function runArtifacts(
  page: Page,
  sessionId: string,
  run: Run,
): Promise<ToolCallDetail> {
  const callsResult = await apiJSON<ToolCallListResponse>(
    page,
    "/api/v1/sessions/" +
      encodeURIComponent(sessionId) +
      "/runs/" +
      encodeURIComponent(run.id) +
      "/tool-calls?limit=100",
  );
  expect(callsResult.status).toBe(200);
  const call = callsResult.body.items.find(
    (item) =>
      item.tool_id === "builtin_skill_execute" ||
      item.model_name === "skill_execute" ||
      item.model_name.endsWith("_skill_execute"),
  );
  expect(call).toBeDefined();
  const detail = await apiJSON<ToolCallDetail>(
    page,
    "/api/v1/sessions/" +
      encodeURIComponent(sessionId) +
      "/runs/" +
      encodeURIComponent(run.id) +
      "/tool-calls/" +
      encodeURIComponent(call?.id as string),
  );
  expect(detail.status).toBe(200);
  return detail.body;
}

function assertDownloadBytes(filename: string, bytes: Buffer): void {
  expect(bytes.length).toBeGreaterThan(0);
  if (filename.toLowerCase().endsWith(".png")) {
    expect(bytes.subarray(0, 8)).toEqual(
      Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    );
  } else if (
    filename.toLowerCase().endsWith(".xlsx") ||
    filename.toLowerCase().endsWith(".docx") ||
    filename.toLowerCase().endsWith(".pptx")
  ) {
    expect(bytes.subarray(0, 2).toString()).toBe("PK");
  } else if (filename.toLowerCase().endsWith(".pdf")) {
    expect(bytes.subarray(0, 4).toString()).toBe("%PDF");
  }
}

async function allConversationMessages(
  page: Page,
  sessionId: string,
): Promise<ConversationMessagePage["items"]> {
  const messages: ConversationMessagePage["items"] = [];
  let beforeSequence: number | undefined;
  while (true) {
    const params = new URLSearchParams({ limit: "100" });
    if (beforeSequence !== undefined) {
      params.set("before_sequence", String(beforeSequence));
    }
    const result = await apiJSON<ConversationMessagePage>(
      page,
      "/api/v1/sessions/" +
        encodeURIComponent(sessionId) +
        "/messages?" +
        params.toString(),
    );
    expect(result.status).toBe(200);
    messages.push(...result.body.items);
    const next = result.body.next_before_sequence;
    if (
      next === null ||
      (beforeSequence !== undefined && next >= beforeSequence)
    ) {
      return messages;
    }
    beforeSequence = next;
  }
}

async function allRunEvents(
  page: Page,
  sessionId: string,
  runId: string,
): Promise<RunEventPage["items"]> {
  const events: RunEventPage["items"] = [];
  let afterSequence = 0;
  while (true) {
    const result = await apiJSON<RunEventPage>(
      page,
      "/api/v1/sessions/" +
        encodeURIComponent(sessionId) +
        "/runs/" +
        encodeURIComponent(runId) +
        "/events?after_sequence=" +
        String(afterSequence) +
        "&limit=100",
    );
    expect(result.status).toBe(200);
    events.push(...result.body.items);
    const next = result.body.next_after_sequence;
    if (next === null || next <= afterSequence) {
      return events;
    }
    afterSequence = next;
  }
}

async function downloadArtifact(
  page: Page,
  testInfo: TestInfo,
  filename: string,
) {
  const button = page.getByRole("button", {
    name: "下载文件：" + filename,
  });
  await expect(button.first()).toBeVisible();
  const downloadPromise = page.waitForEvent("download");
  await button.first().click();
  const download = await downloadPromise;
  const evidence = await saveDownloadEvidence(
    testInfo,
    download,
    "artifact",
  );
  expect(evidence.suggestedFilename).toBe(filename);
  assertDownloadBytes(filename, evidence.bytes);
  return evidence;
}

async function assertPostDeliveryTerminalScenario(
  page: Page,
  state: SkillsLiveState,
  scenario: LiveScenario,
  testInfo: TestInfo,
  evidencePrefix: string,
  versionLabel: string,
  transientMarker: string,
): Promise<void> {
  const sessionId = await startSkillRun(
    page,
    state,
    scenario.prompt,
  );
  const approval = await waitForApproval(
    page,
    sessionId,
    state,
    versionLabel,
  );
  await approval.getByRole("button", { name: "批准" }).click();

  if (scenario.expectedTerminalStatus === "cancelled") {
    await expect(
      page.getByLabel("Athena 的临时流式输出"),
    ).toContainText(transientMarker, { timeout: 60_000 });
    await page.getByRole("button", { name: "停止任务" }).click();
  }

  const terminalRun = await waitForRunStatus(
    page,
    sessionId,
    scenario.expectedTerminalStatus,
  );
  const transient = page.getByLabel("Athena 的临时流式输出");
  await expect(transient).toContainText(transientMarker);
  await expect(transient).toContainText("未完成回答");
  await expect(transient).toContainText("刷新后不承诺恢复");
  await expect(
    page.getByText(/未完成回答已保留在当前页面/),
  ).toBeVisible();

  const selectedSkillIds =
    terminalRun.skill_snapshot?.selected_skill_ids ?? [];
  const fixedSkills =
    terminalRun.skill_snapshot?.skills.filter((skill) =>
      selectedSkillIds.includes(skill.skill_id),
    ) ?? [];
  expect(fixedSkills).toHaveLength(1);
  expect(fixedSkills[0]?.version_label).toBe(versionLabel);
  const expectedArtifacts = expectedArtifactsForVersion(
    scenario.expectedArtifacts,
    fixedSkills[0].version_label,
  );

  const detail = await runArtifacts(page, sessionId, terminalRun);
  expect(
    detail.artifacts.map((artifact) => artifact.filename),
  ).toEqual(expectedArtifacts);
  expect(detail.safe_skill_projection?.skill_version_label).toBe(
    versionLabel,
  );

  const files = await apiJSON<SessionFileListResponse>(
    page,
    "/api/v1/sessions/" +
      encodeURIComponent(sessionId) +
      "/files?limit=100",
  );
  expect(files.status).toBe(200);
  for (const filename of expectedArtifacts) {
    expect(
      files.body.items.some((file) => file.filename === filename),
    ).toBe(true);
  }

  const messages = await allConversationMessages(page, sessionId);
  const deliveryMessages = messages.filter(
    (message) =>
      message.role === "assistant" &&
      message.run_id === terminalRun.id,
  );
  expect(deliveryMessages.length).toBeGreaterThan(0);
  for (const filename of expectedArtifacts) {
    expect(JSON.stringify(deliveryMessages)).toContain(filename);
  }
  const events = await allRunEvents(page, sessionId, terminalRun.id);
  expect(events.some((event) => event.type === "completed")).toBe(false);

  await attachScreenshot(page, testInfo, evidencePrefix + "-retained");
  await page.reload();
  await expect(
    page.locator(".run-summary .run-status").first(),
  ).toHaveText(
    STATUS_LABELS[scenario.expectedTerminalStatus] ??
      scenario.expectedTerminalStatus,
  );
  await expect(
    page.getByLabel("Athena 的临时流式输出"),
  ).toHaveCount(0);
  for (const filename of expectedArtifacts) {
    await expect(
      page
        .locator(".session-file-list li")
        .filter({ hasText: filename }),
    ).toBeVisible();
  }
}

test.describe.serial("Skills 真实浏览器闭环", () => {
  let state: SkillsLiveState;
  let normalSessionId = "";
  let normalVersionLabel = "";

  test.beforeAll(async () => {
    state = await loadSkillsLiveState();
  });

  test("管理员发布技能、普通用户审批下载、刷新恢复和后续引用", async ({
    browser,
  }, testInfo) => {
    const mainScenario = state.scenarios.normal;
    const reuseScenario = state.scenarios.reuse;
    expect(mainScenario.expectedArtifacts).toHaveLength(3);
    await attachSanitizedManifest(testInfo, state);

    const adminContext = await browser.newContext({
      acceptDownloads: true,
      baseURL: state.frontendBaseURL,
    });
    const userContext = await browser.newContext({
      acceptDownloads: true,
      baseURL: state.frontendBaseURL,
    });
    try {
      const adminPage = await adminContext.newPage();
      await loginThroughUi(adminPage, state.admin);
      await adminPage.goto("/admin/skills");
      await expect(
        adminPage.getByRole("heading", { level: 1, name: "技能管理" }),
      ).toBeVisible();

      await adminPage
        .getByRole("button", { name: "导入技能 ZIP" })
        .click();
      let importDialog = adminPage.getByRole("dialog", {
        name: "导入技能包",
      });
      await importDialog
        .getByLabel("技能 ZIP")
        .setInputFiles(await writeInvalidZip(testInfo));
      await importDialog
        .getByRole("button", { name: "导入并查看检查结果" })
        .click();
      await expect(importDialog.getByRole("alert")).toContainText(
        /ZIP|技能包/,
      );
      await importDialog.getByRole("button", { name: "取消" }).click();

      await adminPage
        .getByRole("button", { name: "导入技能 ZIP" })
        .click();
      importDialog = adminPage.getByRole("dialog", {
        name: "导入技能包",
      });
      await importDialog
        .getByLabel("技能 ZIP")
        .setInputFiles(state.skillZipPath);
      await importDialog
        .getByRole("button", { name: "导入并查看检查结果" })
        .click();
      const detailDialog = adminPage.locator(
        ".admin-skill-detail-modal",
      );
      await expect(detailDialog).toBeVisible({ timeout: 60_000 });
      const initialVersion = detailDialog
        .locator(".admin-skill-version-tab")
        .filter({ hasText: "v1" })
        .first();
      await expect(initialVersion).toBeVisible();
      await initialVersion.click();
      await expect(
        detailDialog.getByRole("heading", { name: "操作指南" }),
      ).toBeVisible();
      await expect(
        detailDialog.getByText(state.guideMarker, { exact: false }),
      ).toBeVisible();
      await expect(
        detailDialog.getByRole("heading", { name: "依赖检查" }),
      ).toBeVisible();
      await expect(detailDialog.getByText("依赖满足").first()).toBeVisible();
      const enabled = detailDialog.getByRole("checkbox", {
        name: "向普通用户启用该技能",
      });
      expect(await enabled.isChecked()).toBe(false);
      await enabled.check();
      await detailDialog
        .getByRole("checkbox", {
          name: "每次脚本执行均需用户审批",
        })
        .check();
      await detailDialog
        .getByRole("button", { name: "保存当前配置" })
        .click();
      await expect(
        detailDialog.getByText(/技能配置已保存/),
      ).toBeVisible();
      await attachScreenshot(
        adminPage,
        testInfo,
        "admin-skill-published",
      );

      const skillList = await currentSkills(adminPage);
      const skill = skillList.items.find(
        (item) => item.slug === state.skillSlug,
      );
      expect(skill).toBeDefined();
      expect(skill?.enabled).toBe(true);
      expect(skill?.script_requires_approval).toBe(true);
      const originalVersionLabel = skill?.current_version_label as string;
      expect(originalVersionLabel).toBeTruthy();
      await detailDialog
        .getByRole("button", { name: "关闭", exact: true })
        .click();

      await adminPage
        .getByRole("button", { name: "导入技能 ZIP" })
        .click();
      importDialog = adminPage.getByRole("dialog", {
        name: "导入技能包",
      });
      await importDialog
        .getByLabel("技能 ZIP")
        .setInputFiles(state.skillZipPath);
      await importDialog
        .getByRole("button", { name: "导入并查看检查结果" })
        .click();
      await expect(importDialog.getByRole("alert")).toContainText(
        /slug|已存在/,
      );
      await importDialog.getByRole("button", { name: "取消" }).click();

      const userPage = await userContext.newPage();
      await loginThroughUi(userPage, state.user);
      await userPage.goto("/admin/skills");
      await expect(
        userPage.getByRole("heading", {
          name: "无法访问管理控制台",
        }),
      ).toBeVisible();
      const forbidden = await apiJSON<{ detail: string }>(
        userPage,
        "/api/v1/admin/skills",
      );
      expect(forbidden.status).toBe(403);
      expect(forbidden.body.detail).toBe("admin_required");

      const sessionId = await startSkillRun(
        userPage,
        state,
        mainScenario.prompt,
      );
      normalSessionId = sessionId;
      normalVersionLabel = originalVersionLabel;
      const approval = await waitForApproval(
        userPage,
        sessionId,
        state,
        originalVersionLabel,
      );
      await attachScreenshot(
        userPage,
        testInfo,
        "safe-skill-approval",
      );
      await approval.getByRole("button", { name: "批准" }).click();
      const completedRun = await waitForRunStatus(
        userPage,
        sessionId,
        mainScenario.expectedTerminalStatus,
      );
      expect(completedRun.skill_snapshot?.selected_skill_ids).toContain(
        skill?.id,
      );

      const toolResultDetails = userPage.locator(
        "details.message-tool-ref",
      );
      for (
        let index = 0;
        index < (await toolResultDetails.count());
        index += 1
      ) {
        const item = toolResultDetails.nth(index);
        if (
          (await item.innerText()).includes("工具结果") &&
          (await item.getAttribute("open")) === null
        ) {
          await item.locator("summary").click();
        }
      }
      const completedDetail = await runArtifacts(
        userPage,
        sessionId,
        completedRun,
      );
      expect(
        completedDetail.artifacts.map((artifact) => artifact.filename),
      ).toEqual(mainScenario.expectedArtifacts);
      for (const filename of mainScenario.expectedArtifacts) {
        const evidence = await downloadArtifact(
          userPage,
          testInfo,
          filename,
        );
        if (filename.endsWith(".txt")) {
          const text = evidence.bytes.toString("utf8");
          expect(text).toContain("2026-10");
          expect(text).toContain("42");
        }
      }
      await attachScreenshot(
        userPage,
        testInfo,
        "completed-artifacts",
      );

      await userPage.reload();
      await expect(
        userPage.locator(".run-summary .run-status").first(),
      ).toHaveText(
        STATUS_LABELS[mainScenario.expectedTerminalStatus] ??
          mainScenario.expectedTerminalStatus,
      );
      for (const filename of mainScenario.expectedArtifacts) {
        await expect(
          userPage
            .locator(".session-file-list li")
            .filter({ hasText: filename }),
        ).toBeVisible();
      }

      const reuseArtifact = mainScenario.expectedArtifacts.find(
        (filename) => filename.endsWith(".txt"),
      );
      expect(reuseArtifact).toBe("e2e-summary.txt");
      const artifactFile = await apiJSON<SessionFileListResponse>(
        userPage,
        "/api/v1/sessions/" +
          encodeURIComponent(sessionId) +
          "/files?limit=100",
      );
      expect(artifactFile.status).toBe(200);
      const artifact = artifactFile.body.items.find(
        (item) => item.filename === reuseArtifact,
      );
      expect(artifact).toBeDefined();
      await userPage
        .locator(".session-file-list li")
        .filter({ hasText: reuseArtifact as string })
        .getByRole("checkbox")
        .check();
      await userPage
        .getByLabel("对话草稿")
        .fill(reuseScenario.prompt);
      await userPage
        .getByRole("button", { name: "发送消息" })
        .click();
      const followUpRun = await waitForRunStatus(
        userPage,
        sessionId,
        "completed",
        completedRun.id,
      );
      expect(followUpRun.input_file_ids).toContain(artifact?.id);
      expect(
        followUpRun.skill_snapshot?.selected_skill_ids ?? [],
      ).toEqual([]);
      const followUpCalls = await apiJSON<ToolCallListResponse>(
        userPage,
        "/api/v1/sessions/" +
          encodeURIComponent(sessionId) +
          "/runs/" +
          encodeURIComponent(followUpRun.id) +
          "/tool-calls?limit=100",
      );
      const fileReadCall = followUpCalls.body.items.find(
        (call) => call.model_name === "session_file_read",
      );
      expect(fileReadCall).toBeDefined();
      const fileReadDetail = await apiJSON<ToolCallDetail>(
        userPage,
        "/api/v1/sessions/" +
          encodeURIComponent(sessionId) +
          "/runs/" +
          encodeURIComponent(followUpRun.id) +
          "/tool-calls/" +
          encodeURIComponent(fileReadCall?.id as string),
      );
      expect(fileReadDetail.status).toBe(200);
      expect(fileReadDetail.body.arguments?.file_id).toBe(artifact?.id);
      expect(fileReadDetail.body.outcome?.status).toBe("ok");
      expect(JSON.stringify(fileReadDetail.body.outcome?.data)).toContain(
        "2026-10",
      );
      expect(JSON.stringify(fileReadDetail.body.outcome?.data)).toContain(
        "42",
      );
      const followUpMessages = await apiJSON<ConversationMessagePage>(
        userPage,
        "/api/v1/sessions/" +
          encodeURIComponent(sessionId) +
          "/messages?limit=20",
      );
      const finalAssistant = followUpMessages.body.items.find(
        (message) =>
          message.role === "assistant" &&
          message.run_id === followUpRun.id &&
          message.status === "completed",
      );
      expect(finalAssistant).toBeDefined();
      expect(JSON.stringify(finalAssistant?.parts)).toContain("42");
      const followUpAssistant = userPage.getByRole("article", {
        name:
          "Athena的消息，序号 " + String(finalAssistant?.sequence),
      });
      await expect(followUpAssistant).toBeVisible();
      await expect(followUpAssistant).toContainText("42");
      await expect(
        userPage.getByLabel("Athena 的临时流式输出"),
      ).toHaveCount(0);
      await userPage.reload();
      await expect(followUpAssistant).toBeVisible();
      await expect(followUpAssistant).toContainText("42");
      await attachScreenshot(
        userPage,
        testInfo,
        "follow-up-run-completed",
      );
    } finally {
      await userContext.close();
      await adminContext.close();
    }
  });

  test("停用与版本更新后，待审批旧 Run 继续固定 v1 并完成", async ({
    browser,
  }, testInfo) => {
    const disableScenario = state.scenarios.disable_pin;
    const versionScenario = state.scenarios.version_update_pin;
    expect(normalSessionId).toBeTruthy();
    expect(normalVersionLabel).toBeTruthy();

    const adminContext = await browser.newContext({
      baseURL: state.frontendBaseURL,
    });
    const userContext = await browser.newContext({
      baseURL: state.frontendBaseURL,
    });
    try {
      const adminPage = await adminContext.newPage();
      await loginThroughUi(adminPage, state.admin);
      await adminPage.goto("/admin/skills");
      const adminSkill = (
        await currentSkills(adminPage)
      ).items.find((item) => item.slug === state.skillSlug);
      expect(adminSkill?.current_version_label).toBe(normalVersionLabel);

      const userPage = await userContext.newPage();
      await loginThroughUi(userPage, state.user);

      const disableSessionId = await startSkillRun(
        userPage,
        state,
        disableScenario.prompt,
      );
      let approval = await waitForApproval(
        userPage,
        disableSessionId,
        state,
        normalVersionLabel,
      );
      await setSkillEnabled(adminPage, state, false);
      await userPage.bringToFront();
      await expect(
        pageApprovalVersion(userPage, state, normalVersionLabel),
      ).toBeVisible();
      await approval.getByRole("button", { name: "批准" }).click();
      const disabledRun = await waitForRunStatus(
        userPage,
        disableSessionId,
        disableScenario.expectedTerminalStatus,
      );
      const disabledDetail = await runArtifacts(
        userPage,
        disableSessionId,
        disabledRun,
      );
      expect(
        disabledDetail.artifacts.map((artifact) => artifact.filename),
      ).toEqual(disableScenario.expectedArtifacts);

      await setSkillEnabled(adminPage, state, true);
      const versionSessionId = await startSkillRun(
        userPage,
        state,
        versionScenario.prompt,
      );
      approval = await waitForApproval(
        userPage,
        versionSessionId,
        state,
        normalVersionLabel,
      );
      const versionTwoLabel = await uploadVersionTwo(adminPage, state);
      await userPage.bringToFront();
      await expect(
        pageApprovalVersion(userPage, state, normalVersionLabel),
      ).toBeVisible();
      await approval.getByRole("button", { name: "批准" }).click();
      const pinnedRun = await waitForRunStatus(
        userPage,
        versionSessionId,
        versionScenario.expectedTerminalStatus,
      );
      const pinnedDetail = await runArtifacts(
        userPage,
        versionSessionId,
        pinnedRun,
      );
      expect(
        pinnedDetail.artifacts.map((artifact) => artifact.filename),
      ).toEqual(versionScenario.expectedArtifacts);
      expect(
        pinnedRun.skill_snapshot?.skills[0]?.version_label,
      ).toBe(normalVersionLabel);

      const updatedSkill = (
        await currentSkills(adminPage)
      ).items.find((item) => item.slug === state.skillSlug);
      expect(updatedSkill?.current_version_label).toBe(versionTwoLabel);
      expect(updatedSkill?.current_version_label).not.toBe(
        normalVersionLabel,
      );

      await userPage.goto(
        "/chat/" + encodeURIComponent(normalSessionId),
      );
      await expect(
        userPage.getByText(state.scenarios.normal.prompt),
      ).toBeVisible();
      await expect(
        userPage.getByText(/技能执行完成，已交付 E2E Excel 报表和趋势图/),
      ).toBeVisible();
      await expect(
        userPage.locator(".run-summary .run-status").first(),
      ).toHaveText("已完成");
      await attachScreenshot(
        userPage,
        testInfo,
        "old-run-version-pinned",
      );
    } finally {
      await userContext.close();
      await adminContext.close();
    }
  });

  test("拒绝审批与取消慢脚本均不会发布产物", async ({
    browser,
  }, testInfo) => {
    const rejectScenario = state.scenarios.approval_reject;
    const cancelScenario = state.scenarios.cancel_slow;
    const context = await browser.newContext({
      baseURL: state.frontendBaseURL,
    });
    try {
      const page = await context.newPage();
      await loginThroughUi(page, state.user);
      const currentLabel = (
        await publicSkills(page)
      ).items.find((item) => item.slug === state.skillSlug)
        ?.current_version_label as string;
      expect(currentLabel).toBeTruthy();

      const rejectSessionId = await startSkillRun(
        page,
        state,
        rejectScenario.prompt,
      );
      const rejectApproval = await waitForApproval(
        page,
        rejectSessionId,
        state,
        currentLabel,
      );
      await rejectApproval.getByRole("button", { name: "拒绝" }).click();
      const rejectedRun = await waitForRunStatus(
        page,
        rejectSessionId,
        rejectScenario.expectedTerminalStatus,
      );
      const rejectedDetail = await runArtifacts(
        page,
        rejectSessionId,
        rejectedRun,
      );
      expect(rejectedDetail.artifacts).toEqual([]);
      await expect(
        page.getByText(/审批已拒绝/).first(),
      ).toBeVisible();

      const cancelSessionId = await startSkillRun(
        page,
        state,
        cancelScenario.prompt,
      );
      const cancelApproval = await waitForApproval(
        page,
        cancelSessionId,
        state,
        currentLabel,
      );
      await cancelApproval.getByRole("button", { name: "批准" }).click();
      const executingRun = await latestRun(page, cancelSessionId);
      const executingCall = await waitForExecutingSkillCall(
        page,
        cancelSessionId,
        executingRun.id,
      );
      const startEvidence = await waitForSkillScriptStart(
        state.skillTempRoot,
        [executingCall.id, executingCall.tool_call_id],
      );
      await expect(
        page.locator(".run-summary .run-status").first(),
      ).toHaveText("运行中", { timeout: 60_000 });
      await page.getByRole("button", { name: "停止任务" }).click();
      const cancelledRun = await waitForRunStatus(
        page,
        cancelSessionId,
        cancelScenario.expectedTerminalStatus,
      );
      const cancelledDetail = await runArtifacts(
        page,
        cancelSessionId,
        cancelledRun,
      );
      expect(["cancelled", "unknown"]).toContain(cancelledDetail.status);
      expect(cancelledDetail.artifacts).toEqual([]);
      await waitForSkillScriptCleanup(startEvidence);
      await assertPostDeliveryTerminalScenario(
        page,
        state,
        state.scenarios.post_delivery_failed,
        testInfo,
        "post-delivery-failed",
        currentLabel,
        "POST_DELIVERY_TRANSIENT_FAILED",
      );
      await assertPostDeliveryTerminalScenario(
        page,
        state,
        state.scenarios.post_delivery_cancelled,
        testInfo,
        "post-delivery-cancelled",
        currentLabel,
        "POST_DELIVERY_TRANSIENT_CANCELLED",
      );
      await attachScreenshot(
        page,
        testInfo,
        "reject-and-cancel-evidence",
      );
    } finally {
      await context.close();
    }
  });

  test("技能在选择后失效时阻止创建 Run 并保留草稿", async ({
    browser,
  }, testInfo) => {
    const adminContext = await browser.newContext({
      baseURL: state.frontendBaseURL,
    });
    const userContext = await browser.newContext({
      baseURL: state.frontendBaseURL,
    });
    try {
      const adminPage = await adminContext.newPage();
      await loginThroughUi(adminPage, state.admin);
      await adminPage.goto("/admin/skills");
      const fixtureSkill = (
        await currentSkills(adminPage)
      ).items.find((item) => item.slug === state.skillSlug);
      expect(fixtureSkill).toBeDefined();
      if (!fixtureSkill?.enabled) {
        await setSkillEnabled(adminPage, state, true);
      }
      const userPage = await userContext.newPage();
      await loginThroughUi(userPage, state.user);
      await userPage.goto("/chat");

      const option = userPage
        .locator("label.skill-catalog-option")
        .filter({ hasText: state.skillName })
        .first();
      await expect(option).toBeVisible();
      await option.getByRole("checkbox").check();
      await userPage
        .getByLabel("选择任务文件")
        .setInputFiles(state.csvPath);
      const invalidSelectionDraft =
        "E2E_INVALID_SELECTION 使用已经选择的技能生成报表。";
      await userPage
        .getByLabel("对话草稿")
        .fill(invalidSelectionDraft);

      await setSkillEnabled(adminPage, state, false);
      await userPage.bringToFront();
      await userPage
        .getByRole("button", { name: "发送消息" })
        .click();
      await expect(userPage).toHaveURL(/\/chat\/[^/]+$/);
      await expect(
        userPage.getByText(
          "所选技能已停用、依赖失效或版本不可用，请刷新目录后重新选择。",
        ),
      ).toBeVisible();
      await expect(userPage.getByLabel("对话草稿")).toHaveValue(
        invalidSelectionDraft,
      );
      await expect(
        userPage.getByText(state.csvFilename, { exact: true }).first(),
      ).toBeVisible();
      const sessionId = new URL(userPage.url()).pathname.split("/").at(-1);
      const runs = await apiJSON<RunListResponse>(
        userPage,
        "/api/v1/sessions/" +
          encodeURIComponent(sessionId as string) +
          "/runs?limit=1",
      );
      expect(runs.status).toBe(200);
      expect(runs.body.items).toEqual([]);
      await attachScreenshot(
        userPage,
        testInfo,
        "stale-skill-selection-blocked",
      );
    } finally {
      await userContext.close();
      await adminContext.close();
    }
  });
});
