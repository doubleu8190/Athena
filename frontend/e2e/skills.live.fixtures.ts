import {
  expect,
  type Page,
  type TestInfo,
} from "@playwright/test";
import { readFile, readdir, writeFile } from "node:fs/promises";
import { basename, dirname, join } from "node:path";

import {
  readSkillsLiveContract,
  type LiveAccount,
  type LiveScenarioContract,
} from "./skills.live.contract";

export interface LiveScenario {
  approvalDecision: string;
  expectedArtifacts: string[];
  expectedTerminalStatus: string;
  key: string;
  name: string;
  prompt: string;
}

export interface SkillsLiveState {
  admin: LiveAccount;
  apiBaseURL: string;
  csvPath: string;
  csvFilename: string;
  csvExpectedText: string;
  frontendBaseURL: string;
  guideMarker: string;
  scenarios: Record<string, LiveScenario>;
  skillTempRoot: string;
  skillName: string;
  skillScriptPath: string;
  skillSlug: string;
  skillZipPath: string;
  stateFile: string;
  user: LiveAccount;
  versionTwoZipPath: string;
}

function toScenario(
  scenario: LiveScenarioContract,
  key: string,
): LiveScenario {
  return {
    approvalDecision: scenario.approval_decision,
    expectedArtifacts: [...scenario.expected_artifacts],
    expectedTerminalStatus: scenario.expected_terminal_status,
    key,
    name: scenario.name,
    prompt: scenario.prompt,
  };
}

export async function loadSkillsLiveState(): Promise<SkillsLiveState> {
  const manifest = readSkillsLiveContract();
  const scenarios: Record<string, LiveScenario> = {
    approval_reject: toScenario(
      manifest.scenarios.approval_reject,
      "approval_reject",
    ),
    cancel_slow: toScenario(
      manifest.scenarios.cancel_slow,
      "cancel_slow",
    ),
    disable_pin: toScenario(
      manifest.scenarios.disable_pin,
      "disable_pin",
    ),
    normal: toScenario(manifest.scenarios.normal, "normal"),
    post_delivery_cancelled: toScenario(
      manifest.scenarios.post_delivery_cancelled,
      "post_delivery_cancelled",
    ),
    post_delivery_failed: toScenario(
      manifest.scenarios.post_delivery_failed,
      "post_delivery_failed",
    ),
    reuse: toScenario(manifest.scenarios.reuse, "reuse"),
    version_update_pin: toScenario(
      manifest.scenarios.version_update_pin,
      "version_update_pin",
    ),
  };

  return {
    admin: manifest.accounts.admin,
    apiBaseURL: manifest.api_base_url,
    csvPath: manifest.input.csv_path,
    csvExpectedText: manifest.input.expected_text,
    csvFilename: manifest.input.filename,
    frontendBaseURL: manifest.frontend_base_url,
    guideMarker: manifest.skill.guide_marker,
    scenarios,
    skillTempRoot: manifest.resources.skill_temp_root,
    skillName: manifest.skill.name,
    skillScriptPath: manifest.skill.script_path,
    skillSlug: manifest.skill.slug,
    skillZipPath: manifest.skill.zip_path,
    stateFile: manifest.state_file,
    user: manifest.accounts.ordinary,
    versionTwoZipPath: manifest.skill.version_two_zip_path,
  };
}

export async function loginThroughUi(
  page: Page,
  account: LiveAccount,
): Promise<void> {
  await page.goto("/login");
  await expect(
    page.getByRole("heading", { level: 1, name: "登录工作台" }),
  ).toBeVisible();

  // 等待登录配置加载完成（出现折叠触发按钮或直接展开的邮箱输入框），避免骨架屏阶段误判。
  const localToggle = page.getByRole("button", {
    name: "管理员 / 应急邮箱密码登录",
  });
  const emailInput = page.getByLabel("工作邮箱");
  await expect(localToggle.or(emailInput)).toBeVisible();

  // 当飞书 SSO 或 Mock 模式启用时，本地应急邮箱密码表单默认折叠，需先展开后再填表登录。
  if (await localToggle.isVisible()) {
    const expanded = await localToggle.getAttribute("aria-expanded");
    if (expanded !== "true") {
      await localToggle.click();
    }
  }

  await emailInput.fill(account.email);
  await page.getByLabel("密码", { exact: true }).fill(account.password);
  await page.getByRole("button", { name: "使用邮箱登录" }).click();
  await expect(page).not.toHaveURL(/\/login(?:[/?#]|$)/);
  await expect(
    page.getByRole("complementary", { name: "工作区侧栏" }).or(
      page.getByRole("button", { name: "打开导航菜单" }),
    ),
  ).toBeVisible();
}

export async function apiJSON<T>(
  page: Page,
  path: string,
): Promise<{ body: T; status: number }> {
  const response = await page.context().request.get(path, {
    failOnStatusCode: false,
  });
  const contentType = response.headers()["content-type"] ?? "";
  const body = contentType.includes("application/json")
    ? ((await response.json()) as T)
    : ((await response.text()) as T);
  return { body, status: response.status() };
}

export async function attachSanitizedManifest(
  testInfo: TestInfo,
  state: SkillsLiveState,
): Promise<void> {
  const summary = {
    accounts: {
      admin: { email: state.admin.email, password: "<redacted>" },
      user: { email: state.user.email, password: "<redacted>" },
    },
    assets: {
      csv: basename(state.csvPath),
      scenarios: Object.values(state.scenarios).map((scenario) => ({
        expected_artifacts: scenario.expectedArtifacts,
        name: scenario.name,
      })),
      skill_zip: basename(state.skillZipPath),
      version_two_zip: basename(state.versionTwoZipPath),
    },
    frontend_base_url: state.frontendBaseURL,
    state_file: state.stateFile,
  };
  const path = testInfo.outputPath("sanitized-live-manifest.json");
  await writeFile(path, JSON.stringify(summary, null, 2), "utf8");
  await testInfo.attach("sanitized-live-manifest.json", {
    contentType: "application/json",
    path,
  });
}

export async function attachScreenshot(
  page: Page,
  testInfo: TestInfo,
  name: string,
): Promise<void> {
  const path = testInfo.outputPath(name + ".png");
  await page.screenshot({ fullPage: true, path });
  await testInfo.attach(name + ".png", {
    contentType: "image/png",
    path,
  });
}

export async function saveDownloadEvidence(
  testInfo: TestInfo,
  download: {
    saveAs: (path: string) => Promise<void>;
    suggestedFilename: () => string;
  },
  prefix: string,
): Promise<{ bytes: Buffer; path: string; suggestedFilename: string }> {
  const suggestedFilename = download.suggestedFilename();
  const safeName = basename(suggestedFilename).replace(
    /[^A-Za-z0-9._-]/g,
    "_",
  );
  const path = testInfo.outputPath(prefix + "-" + safeName);
  await download.saveAs(path);
  const bytes = await readFile(path);
  await testInfo.attach(prefix + "-" + safeName, {
    body: bytes,
    contentType: "application/octet-stream",
  });
  return { bytes, path, suggestedFilename };
}

export async function writeInvalidZip(testInfo: TestInfo): Promise<string> {
  const path = testInfo.outputPath("invalid-skill.zip");
  await writeFile(path, "this is not a zip archive", "utf8");
  return path;
}

export interface SkillScriptStartEvidence {
  directory: string;
  directoryName: string;
  pid: number;
}

function processIsAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code !== "ESRCH";
  }
}

export async function waitForSkillScriptStart(
  skillTempRoot: string,
  callIds: string[],
  timeoutMs = 60_000,
): Promise<SkillScriptStartEvidence> {
  const prefixes = callIds.map(
    (callId) => "skill-" + callId.slice(0, 32) + "-",
  );
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const entries = await readdir(skillTempRoot, {
      withFileTypes: true,
    }).catch((error: NodeJS.ErrnoException) => {
      if (error.code === "ENOENT") {
        return [];
      }
      throw error;
    });
    const directory = entries.find(
      (entry) =>
        entry.isDirectory() &&
        prefixes.some((prefix) => entry.name.startsWith(prefix)),
    );
    if (directory) {
      const marker = await readFile(
        join(skillTempRoot, directory.name, ".e2e-script-started"),
        "utf8",
      ).catch((error: NodeJS.ErrnoException) => {
        if (error.code === "ENOENT") {
          return null;
        }
        throw error;
      });
      if (marker) {
        const pidMatch = /pid=(\d+)/.exec(marker);
        const cwdMatch = /cwd=([A-Za-z0-9_.-]+)/.exec(marker);
        if (!pidMatch || cwdMatch?.[1] !== directory.name) {
          throw new Error("skill start marker 缺少有效 pid 或 cwd");
        }
        return {
          directory: join(skillTempRoot, directory.name),
          directoryName: directory.name,
          pid: Number(pidMatch[1]),
        };
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("等待 skill 脚本 start marker 超时");
}

export async function waitForSkillScriptCleanup(
  evidence: SkillScriptStartEvidence,
  timeoutMs = 30_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const entries = await readdir(
      dirname(evidence.directory),
      { withFileTypes: true },
    ).catch((error: NodeJS.ErrnoException) => {
      if (error.code === "ENOENT") {
        return [];
      }
      throw error;
    });
    const directoryExists = entries.some(
      (entry) => entry.name === evidence.directoryName,
    );
    if (!directoryExists && !processIsAlive(evidence.pid)) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("取消后 skill 工作目录或脚本进程仍未收敛");
}
