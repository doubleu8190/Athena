import { describe, expect, it } from "vitest";

import { makeMcpServer, makeModelProfile } from "../../test/adminFixtures";
import {
  compressionPolicyBudgets,
  defaultModelProfileForm,
  extractToolSchemaParamNames,
  mcpServerForm,
  modelProfileForm,
  subagentForm,
  validateCompressionPolicyForm,
  validateMcpServerForm,
  validateMcpServerPatchForm,
  validateModelProfileEditForm,
  validateModelProfileForm,
  validateSubagentForm,
  validateSystemPromptContent,
  validateToolMetadataForm,
  type McpServerFormValues,
  type SubagentFormValues,
  type SubagentProfile,
} from "./model";

function validSubagentFormValues(): SubagentFormValues {
  return {
    slug: "research_assistant",
    name: "调研助手",
    description: "检索资料并整理摘要。",
    system_prompt: "你是严谨的调研助手。",
    model_profile_id: "",
    allowed_tool_ids: ["builtin_knowledge_search"],
    enabled: true,
    max_llm_requests: "",
    timeout_seconds: "",
    visibility_scope: "company",
    owner_department_id: "",
    visibility_grants: [],
  };
}

function validateSubagentSlug(slug: string) {
  return validateSubagentForm({
    ...validSubagentFormValues(),
    slug,
  });
}

describe("管理表单纯校验", () => {
  it("模型表单拒绝非 HTTPS、容量倒挂和工具并行依赖", () => {
    const values = defaultModelProfileForm();
    values.name = "审核模型";
    values.model = "atlas-review";
    values.base_url = "http://llm.example/v1";
    values.api_key = "test-key";
    values.context_window_tokens = "100";
    values.max_output_tokens = "200";
    values.supports_parallel_tool_calls = true;
    values.supports_tools = false;

    const invalid = validateModelProfileForm(values);
    expect(invalid.input).toBeNull();
    expect(invalid.errors.base_url).toContain("HTTPS");
    expect(invalid.errors.max_output_tokens).toContain("不能超过");
    expect(
      invalid.errors.supports_parallel_tool_calls,
    ).toContain("工具支持");

    values.base_url = "https://llm.example/v1";
    values.context_window_tokens = "100000";
    values.max_output_tokens = "8192";
    values.supports_tools = true;
    const valid = validateModelProfileForm(values);
    expect(valid.input).toMatchObject({
      base_url: "https://llm.example/v1",
      supports_parallel_tool_calls: true,
      supports_tools: true,
    });
  });

  it("上下文窗口上限为 1,048,576，最大输出仍使用独立上限", () => {
    const values = defaultModelProfileForm();
    values.name = "大上下文模型";
    values.model = "large-context";
    values.base_url = "https://llm.example/v1";
    values.api_key = "test-key";
    values.context_window_tokens = "1048576";
    values.max_output_tokens = "1000000";
    expect(validateModelProfileForm(values).input?.context_window_tokens).toBe(
      1_048_576,
    );

    values.context_window_tokens = "1048577";
    expect(
      validateModelProfileForm(values).errors.context_window_tokens,
    ).toContain("1–1,048,576");

    values.context_window_tokens = "1048576";
    values.max_output_tokens = "1000001";
    expect(
      validateModelProfileForm(values).errors.max_output_tokens,
    ).toBeDefined();
  });

  it("压缩策略只接受已启用模型、正整数 Token 和合法比例", () => {
    const enabled = makeModelProfile({
      id: "model-enabled",
      name: "已启用模型",
    });
    const disabled = makeModelProfile({
      enabled: false,
      id: "model-disabled",
      name: "已停用模型",
    });
    const values = {
      compression_model_profile_id: disabled.id,
      recent_ratio: "0.6",
      recent_token_cap: "0",
      summary_ratio: "0.5",
      summary_token_cap: "8192.5",
      trigger_tokens: "262144",
    };

    const invalid = validateCompressionPolicyForm(values, [
      enabled,
      disabled,
    ]);
    expect(invalid.input).toBeNull();
    expect(invalid.errors.compression_model_profile_id).toContain(
      "已启用",
    );
    expect(invalid.errors.summary_ratio).toContain("之和");
    expect(invalid.errors.recent_token_cap).toContain("正整数");
    expect(invalid.errors.summary_token_cap).toContain("正整数");

    values.compression_model_profile_id = enabled.id;
    values.recent_ratio = "0.2";
    values.recent_token_cap = "32768";
    values.summary_ratio = "0.1";
    values.summary_token_cap = "8192";
    values.trigger_tokens = "262144";
    expect(
      validateCompressionPolicyForm(values, [enabled, disabled])
        .input,
    ).toEqual({
      compression_model_profile_id: enabled.id,
      recent_ratio: 0.2,
      recent_token_cap: 32768,
      summary_ratio: 0.1,
      summary_token_cap: 8192,
      trigger_tokens: 262144,
    });
  });

  it("派生预算使用当前触发 Token、实际比例和当前上限的最小值", () => {
    expect(
      compressionPolicyBudgets({
        recent_ratio: 0.2,
        recent_token_cap: 32768,
        summary_ratio: 0.1,
        summary_token_cap: 8192,
        trigger_tokens: 262144,
      }),
    ).toEqual({
      recent: 32768,
      summary: 8192,
    });
    const customBudgets = compressionPolicyBudgets({
      recent_ratio: 0.55,
      recent_token_cap: 200,
      summary_ratio: 0.25,
      summary_token_cap: 40,
      trigger_tokens: 100,
    });
    expect(customBudgets.recent).toBeCloseTo(55);
    expect(customBudgets.summary).toBeCloseTo(25);
  });

  it("MCP 表单校验协议秘密名、stdin 环境变量与完整替换", () => {
    const baseFormula: McpServerFormValues = {
      argsText: "",
      command: "",
      cwd: "",
      name: "MCP",
      secrets: [],
      transport: "streamable_http",
      url: "https://mcp.example/mcp",
      visibility_scope: "company",
      owner_department_id: "",
      visibility_grants: [],
      rediscover: false,
    };
    const reserved = validateMcpServerForm({
      ...baseFormula,
      secrets: [
        {
          existing: false,
          id: "secret-1",
          name: "Content-Type",
          value: "value",
        },
      ],
    });
    expect(reserved.input).toBeNull();
    expect(reserved.errors.secrets).toContain("协议保留名");

    const duplicate = validateMcpServerForm({
      ...baseFormula,
      secrets: [
        {
          existing: false,
          id: "secret-1",
          name: "Authorization",
          value: "one",
        },
        {
          existing: false,
          id: "secret-2",
          name: "authorization",
          value: "two",
        },
      ],
    });
    expect(duplicate.errors.secrets).toContain("重复");

    const missingExistingValue = validateMcpServerForm({
      ...baseFormula,
      secrets: [
        {
          existing: true,
          id: "secret-existing",
          name: "Authorization",
          value: "",
        },
      ],
    });
    expect(missingExistingValue.errors.secrets).toContain(
      "重新输入",
    );

    const stdio = validateMcpServerForm({
      ...baseFormula,
      command: "bin/mcp",
      cwd: "relative",
      secrets: [
        {
          existing: false,
          id: "secret-env",
          name: "api-token",
          value: "value",
        },
      ],
      transport: "stdio",
    });
    expect(stdio.errors.command).toContain("绝对路径");
    expect(stdio.errors.cwd).toContain("绝对路径");
    expect(stdio.errors.secrets).toContain("环境变量");
  });

  it("编辑表单保留已有秘密名称但不回显值", () => {
    const server = makeMcpServer({
      id: "mcp-secret",
      name: "秘密服务",
    });
    const form = mcpServerForm(server);
    expect(form.secrets).toEqual([
      {
        existing: true,
        hasValue: true,
        id: "existing-0-Authorization",
        name: "Authorization",
        originalName: "Authorization",
        value: "",
      },
    ]);
  });

  it("系统提示词按 Unicode 字符计数，拒绝全空白和超过十万字符", () => {
    expect(validateSystemPromptContent(" \n\t ")).toContain("空白");
    expect(
      validateSystemPromptContent("😀".repeat(100_000)),
    ).toBeNull();
    expect(
      validateSystemPromptContent("a".repeat(100_001)),
    ).toContain("100,000");
  });

  it("子智能体 slug 保留字与后端领域规则一致", () => {
    for (const reserved of ["main", "default", "system", "subagent"]) {
      expect(validateSubagentSlug(reserved).errors.slug).toContain(
        "系统保留字",
      );
    }

    for (const allowed of [
      "admin",
      "athena",
      "dispatch_subagents",
      "root",
      "self",
      "subagents",
    ]) {
      expect(validateSubagentSlug(allowed).errors.slug).toBeUndefined();
    }
  });

  it("子智能体文本按 Unicode 码点校验后端边界", () => {
    const values = validSubagentFormValues();
    values.name = "😀".repeat(100);
    values.description = "😀".repeat(1_000);
    values.system_prompt = "😀".repeat(32_000);

    expect(validateSubagentForm(values)).toEqual({
      createInput: expect.objectContaining({
        name: values.name,
        description: values.description,
        system_prompt: values.system_prompt,
      }),
      errors: {},
    });

    values.name = "😀".repeat(101);
    expect(validateSubagentForm(values).errors.name).toContain("1–100");

    values.name = "调研助手";
    values.description = "😀".repeat(1_001);
    expect(validateSubagentForm(values).errors.description).toContain(
      "1–1,000",
    );

    values.description = "描述";
    values.system_prompt = "😀".repeat(32_001);
    expect(validateSubagentForm(values).errors.system_prompt).toContain(
      "1–32,000",
    );
  });

  it("子智能体文本仅允许后端规定的空白控制字符", () => {
    const values = validSubagentFormValues();
    values.description = "第一行\n第二行\r制表\t字符\u0085结束";
    values.system_prompt = "第一行\n第二行\r制表\t字符\u0085结束";
    const c1Allowed = validateSubagentForm(values);
    expect(c1Allowed.createInput?.description).toBe(
      "第一行\n第二行\r制表\t字符\u0085结束",
    );
    expect(c1Allowed.createInput?.system_prompt).toBe(
      "第一行\n第二行\r制表\t字符\u0085结束",
    );

    values.name = "调研\n助手";
    expect(validateSubagentForm(values).errors.name).toContain(
      "C0 控制字符",
    );

    values.name = "调研助手";
    values.description = "包含\u0000控制字符";
    expect(validateSubagentForm(values).errors.description).toContain(
      "DEL（U+007F）",
    );

    values.description = "正常描述";
    values.system_prompt = "包含\u007f控制字符";
    expect(validateSubagentForm(values).errors.system_prompt).toContain(
      "DEL（U+007F）",
    );

    values.system_prompt = "包含\u001d控制字符";
    expect(validateSubagentForm(values).errors.system_prompt).toContain(
      "C0 控制字符",
    );
  });

  it("子智能体按 Python strip 边界规范化并提交与后端一致的值", () => {
    const values = validSubagentFormValues();
    values.slug = "\u0085research_assistant\u001c";
    values.name = "\u0085调研助手\u0085";
    values.description = "\u001c第一行\r\n第二行\u0085";
    values.system_prompt = "\ufeff\u0085只使用已核验资料\u0085";
    values.model_profile_id = "\u0085model_1\u001c";
    values.allowed_tool_ids = ["\u0085tool_1\u001c", "tool_1"];

    const result = validateSubagentForm(values);
    expect(result.errors).toEqual({});
    expect(result.createInput).toMatchObject({
      slug: "research_assistant",
      name: "调研助手",
      description: "第一行\r\n第二行",
      system_prompt: "\ufeff\u0085只使用已核验资料",
      model_profile_id: "model_1",
      allowed_tool_ids: ["tool_1"],
    });

    values.description = "\u0085";
    expect(validateSubagentForm(values).errors.description).toContain(
      "1–1,000",
    );
  });

  it("工具白名单去重后最多 100 项并保留编辑数据", () => {
    const values = validSubagentFormValues();
    const toolIds = Array.from({ length: 100 }, (_, index) => `tool_${index}`);
    values.allowed_tool_ids = [...toolIds, " tool_0 "];

    expect(validateSubagentForm(values).createInput?.allowed_tool_ids).toEqual(
      toolIds,
    );

    values.allowed_tool_ids = [...toolIds, "tool_100"];
    const tooMany = validateSubagentForm(values);
    expect(tooMany.createInput).toBeNull();
    expect(tooMany.errors.allowed_tool_ids).toContain("最多只能选择 100 项");
    expect(tooMany.errors.allowed_tool_ids).toContain("当前已选择 101 项");
  });

  it("编辑态完整回填子智能体字段且复制工具数组", () => {
    const profile: SubagentProfile = {
      id: "subagent-edit",
      slug: "research_assistant",
      name: "已有调研助手",
      description: "已有描述",
      system_prompt: "已有提示词",
      model_profile_id: "model-1",
      allowed_tool_ids: ["tool_1", "tool_2"],
      enabled: true,
      max_llm_requests: 12,
      timeout_seconds: 180,
      version: 3,
      created_by: "admin-1",
      created_at: "2026-11-16T00:00:00Z",
      owner_department_id: null,
      updated_at: "2026-11-16T00:00:00Z",
      visibility_grants: [],
      visibility_scope: "company",
    };

    const form = subagentForm(profile);
    expect(form).toMatchObject({
      slug: profile.slug,
      name: profile.name,
      description: profile.description,
      system_prompt: profile.system_prompt,
      model_profile_id: profile.model_profile_id,
      allowed_tool_ids: profile.allowed_tool_ids,
      max_llm_requests: "12",
      timeout_seconds: "180",
    });

    form.allowed_tool_ids.push("tool_3");
    expect(profile.allowed_tool_ids).toEqual(["tool_1", "tool_2"]);
  });

  it("模型编辑校验支持留空 api_key 保留原密文与可选场景字段归空", () => {
    const profile = makeModelProfile({
      id: "model-edit",
      name: "日常模型",
      scenario_label: "日常办公",
      scenario_description: "处理常规问答",
      supports_vision: true,
      visible_to_users: false,
      version: 4,
    });
    const form = modelProfileForm(profile);
    expect(form.api_key).toBe("");
    form.scenario_label = "   ";
    form.scenario_description = "";
    form.visible_to_users = true;

    const editWithoutKey = validateModelProfileEditForm(form, profile.version);
    expect(editWithoutKey.input).toMatchObject({
      expected_version: 4,
      scenario_label: null,
      scenario_description: null,
      supports_vision: true,
      visible_to_users: true,
    });
    expect(editWithoutKey.input).not.toHaveProperty("api_key");

    form.api_key = "new-secret-key";
    const editWithKey = validateModelProfileEditForm(form, profile.version);
    expect(editWithKey.input?.api_key).toBe("new-secret-key");
  });

  it("MCP 局部更新校验正确区分保留、更新、删除与同名替换凭据", () => {
    const server = makeMcpServer({
      id: "mcp-patch",
      name: "测试网关",
      secrets: [
        { has_value: true, name: "Authorization" },
        { has_value: true, name: "X-Legacy" },
      ],
    });
    const form = mcpServerForm(server);
    // 保留 Authorization，删除 X-Legacy，并新增 X-Trace
    form.secrets = [
      form.secrets[0],
      {
        id: "new-1",
        name: "X-Trace",
        value: "trace-123",
        existing: false,
      },
    ];
    form.rediscover = true;

    const patch = validateMcpServerPatchForm(form, server, server.config_version);
    expect(patch.hasChanges).toBe(true);
    expect(patch.input).toEqual({
      expected_config_version: 1,
      secrets_upsert: { "X-Trace": "trace-123" },
      secrets_remove: ["X-Legacy"],
      rediscover: true,
    });
  });

  it("工具展示校验支持提取 Schema 参数、出参白名单校验与空值归一化", () => {
    expect(
      extractToolSchemaParamNames({
        type: "object",
        properties: { payee: { type: "string" }, amount: { type: "number" } },
      }),
    ).toEqual(["payee", "amount"]);

    const validated = validateToolMetadataForm({
      display_name: "  ",
      approval_summary_template: "向 {payee} 付款 {amount}",
      parameter_labels: [
        { id: "1", key: "payee", label: "收款人" },
        { id: "2", key: "", label: "" },
      ],
      result_labels: [
        { id: "r1", key: " order_no ", label: " 审批单号 " },
        { id: "r2", key: "", label: "" },
      ],
    });
    expect(validated.input).toEqual({
      display_name: null,
      approval_summary_template: "向 {payee} 付款 {amount}",
      parameter_labels: { payee: "收款人" },
      result_labels: { order_no: "审批单号" },
    });

    // 出参白名单校验：重复键、控制字符、超限均拦截
    expect(
      validateToolMetadataForm({
        display_name: "",
        approval_summary_template: "",
        parameter_labels: [],
        result_labels: [
          { id: "r1", key: "order_no", label: "单号" },
          { id: "r2", key: " order_no ", label: "重复单号" },
        ],
      }).errors.result_labels,
    ).toContain("重复");

    expect(
      validateToolMetadataForm({
        display_name: "",
        approval_summary_template: "",
        parameter_labels: [],
        result_labels: [
          { id: "r1", key: "order_no\u0007", label: "单号" },
        ],
      }).errors.result_labels,
    ).toContain("控制字符");

    expect(
      validateToolMetadataForm({
        display_name: "",
        approval_summary_template: "",
        parameter_labels: [],
        result_labels: Array.from({ length: 51 }, (_, index) => ({
          id: `r-${index}`,
          key: `field_${index}`,
          label: `字段${index}`,
        })),
      }).errors.result_labels,
    ).toContain("最多配置 50 项");
  });
});
