import { describe, expect, it } from "vitest";

import { makeMcpServer, makeModelProfile } from "../../test/adminFixtures";
import {
  defaultModelProfileForm,
  mcpServerForm,
  validateCompressionPolicyForm,
  validateMcpServerForm,
  validateModelProfileForm,
  validateSystemPromptContent,
  type McpServerFormValues,
} from "./model";

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

  it("压缩策略只接受已启用模型和合法比例", () => {
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
      summary_ratio: "0.5",
      trigger_ratio: "0.8",
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

    values.compression_model_profile_id = enabled.id;
    values.recent_ratio = "0.2";
    values.summary_ratio = "0.1";
    expect(
      validateCompressionPolicyForm(values, [enabled, disabled])
        .input,
    ).toEqual({
      compression_model_profile_id: enabled.id,
      recent_ratio: 0.2,
      summary_ratio: 0.1,
      trigger_ratio: 0.8,
    });
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
        id: "existing-0-Authorization",
        name: "Authorization",
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
});
