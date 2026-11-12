import { readFileSync } from "node:fs";

export interface LiveAccount {
  email: string;
  password: string;
}

export interface LiveScenarioContract {
  approval_decision: string;
  expected_artifacts: string[];
  expected_terminal_status: string;
  name: string;
  prompt: string;
}

export interface SkillsLiveContract {
  accounts: {
    admin: LiveAccount & { display_name: string };
    ordinary: LiveAccount & { display_name: string };
  };
  api_base_url: string;
  contract_version: 1;
  frontend_base_url: string;
  input: {
    csv_path: string;
    expected_text: string;
    filename: string;
  };
  resources: {
    database_schema: string;
    minio_bucket: string;
    redis_container: string;
    skill_temp_root: string;
  };
  scenarios: {
    approval_reject: LiveScenarioContract;
    cancel_slow: LiveScenarioContract;
    disable_pin: LiveScenarioContract;
    normal: LiveScenarioContract;
    post_delivery_cancelled: LiveScenarioContract;
    post_delivery_failed: LiveScenarioContract;
    reuse: LiveScenarioContract;
    version_update_pin: LiveScenarioContract;
  };
  skill: {
    guide_marker: string;
    name: string;
    script_path: string;
    slug: string;
    version_two_zip_path: string;
    zip_path: string;
  };
  state_file: string;
}

type UnknownRecord = Record<string, unknown>;

function objectValue(value: unknown, field: string): UnknownRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("live manifest 字段 " + field + " 必须是对象");
  }
  return value as UnknownRecord;
}

function stringValue(source: UnknownRecord, key: string): string {
  const value = source[key];
  if (typeof value !== "string" || !value.trim()) {
    throw new Error("live manifest 缺少字符串字段 " + key);
  }
  return value.trim();
}

function stringArrayValue(
  source: UnknownRecord,
  key: string,
): string[] {
  const value = source[key];
  if (
    !Array.isArray(value) ||
    value.some((item) => typeof item !== "string" || !item.trim())
  ) {
    throw new Error("live manifest 字段 " + key + " 必须是字符串数组");
  }
  return value.map((item) => (item as string).trim());
}

function accountValue(
  value: unknown,
  field: string,
): LiveAccount & { display_name: string } {
  const account = objectValue(value, field);
  return {
    display_name: stringValue(account, "display_name"),
    email: stringValue(account, "email"),
    password: stringValue(account, "password"),
  };
}

function scenarioValue(
  value: unknown,
  field: string,
): LiveScenarioContract {
  const scenario = objectValue(value, field);
  return {
    approval_decision: stringValue(scenario, "approval_decision"),
    expected_artifacts: stringArrayValue(
      scenario,
      "expected_artifacts",
    ),
    expected_terminal_status: stringValue(
      scenario,
      "expected_terminal_status",
    ),
    name: stringValue(scenario, "name"),
    prompt: stringValue(scenario, "prompt"),
  };
}

export function readSkillsLiveContract(): SkillsLiveContract {
  const stateFile = process.env.ATHENA_E2E_STATE_FILE;
  if (!stateFile) {
    throw new Error(
      "缺少 ATHENA_E2E_STATE_FILE；拒绝连接未声明的旧运行环境。",
    );
  }
  const raw = objectValue(
    JSON.parse(readFileSync(stateFile, "utf8")),
    "root",
  );
  if (raw.contract_version !== 1) {
    throw new Error("不支持的 live manifest contract_version");
  }
  const accounts = objectValue(raw.accounts, "accounts");
  const skill = objectValue(raw.skill, "skill");
  const input = objectValue(raw.input, "input");
  const resources = objectValue(raw.resources, "resources");
  const scenarios = objectValue(raw.scenarios, "scenarios");
  const manifestStateFile = stringValue(raw, "state_file");
  if (manifestStateFile !== stateFile) {
    throw new Error("live manifest state_file 与 ATHENA_E2E_STATE_FILE 不一致");
  }
  return {
    accounts: {
      admin: accountValue(accounts.admin, "accounts.admin"),
      ordinary: accountValue(
        accounts.ordinary,
        "accounts.ordinary",
      ),
    },
    api_base_url: stringValue(raw, "api_base_url"),
    contract_version: 1,
    frontend_base_url: stringValue(raw, "frontend_base_url"),
    input: {
      csv_path: stringValue(input, "csv_path"),
      expected_text: stringValue(input, "expected_text"),
      filename: stringValue(input, "filename"),
    },
    resources: {
      database_schema: stringValue(
        resources,
        "database_schema",
      ),
      minio_bucket: stringValue(resources, "minio_bucket"),
      redis_container: stringValue(
        resources,
        "redis_container",
      ),
      skill_temp_root: stringValue(
        resources,
        "skill_temp_root",
      ),
    },
    scenarios: {
      approval_reject: scenarioValue(
        scenarios.approval_reject,
        "scenarios.approval_reject",
      ),
      cancel_slow: scenarioValue(
        scenarios.cancel_slow,
        "scenarios.cancel_slow",
      ),
      disable_pin: scenarioValue(
        scenarios.disable_pin,
        "scenarios.disable_pin",
      ),
      normal: scenarioValue(
        scenarios.normal,
        "scenarios.normal",
      ),
      post_delivery_cancelled: scenarioValue(
        scenarios.post_delivery_cancelled,
        "scenarios.post_delivery_cancelled",
      ),
      post_delivery_failed: scenarioValue(
        scenarios.post_delivery_failed,
        "scenarios.post_delivery_failed",
      ),
      reuse: scenarioValue(
        scenarios.reuse,
        "scenarios.reuse",
      ),
      version_update_pin: scenarioValue(
        scenarios.version_update_pin,
        "scenarios.version_update_pin",
      ),
    },
    skill: {
      guide_marker: stringValue(skill, "guide_marker"),
      name: stringValue(skill, "name"),
      script_path: stringValue(skill, "script_path"),
      slug: stringValue(skill, "slug"),
      version_two_zip_path: stringValue(
        skill,
        "version_two_zip_path",
      ),
      zip_path: stringValue(skill, "zip_path"),
    },
    state_file: manifestStateFile,
  };
}
