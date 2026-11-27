import type { ApiSchemas } from "../../shared/api/client";
import { countCodePoints } from "../../shared/lib/text";

export const CUSTOM_INSTRUCTIONS_MAX_LENGTH = 4000;
export const USER_MEMORY_CONTENT_MAX_LENGTH = 500;
export const USER_MEMORY_DEFAULT_LIMIT = 50;
export const USER_MEMORY_WARNING_THRESHOLD = 45;

// 直连 OpenAPI 个人偏好与显式记忆契约。
export type UserWorkProfile = ApiSchemas["UserWorkProfileResponse"];
export type UpdateUserWorkProfileInput =
  ApiSchemas["UpdateUserWorkProfileRequest"];
export type UserMemory = ApiSchemas["UserMemoryResponse"];
export type UserMemorySource = UserMemory["source"];
export type UserMemoryListResponse = ApiSchemas["UserMemoryListResponse"];
export type CreateUserMemoryInput = ApiSchemas["CreateUserMemoryRequest"];
export type UpdateUserMemoryInput = ApiSchemas["UpdateUserMemoryRequest"];

export interface WorkInstructionPreset {
  id: string;
  label: string;
  text: string;
}

/** 面向企业日常办公场景的 3 组长期工作指令参考预设，点击即可填入或换行追加。 */
export const WORK_INSTRUCTION_PRESETS: readonly WorkInstructionPreset[] = [
  {
    id: "conclusion-first",
    label: "结论先行与分点表达",
    text: "请先给核心结论和建议行动，再补充制度依据或关键数据，尽量使用简洁的中文分点列出。",
  },
  {
    id: "finance-metrics",
    label: "财务口径与表格呈现",
    text: "涉及金额和经营数据时，默认以万元为单位并注明统计口径，多项对比优先采用表格形式呈现。",
  },
  {
    id: "business-writing",
    label: "汇报与公文写作风格",
    text: "起草周报、邮件与评审方案时，保持专业克制的企业办公语气，结构突出背景、进展、风险与下一步计划。",
  },
];

// 与后端 domain.preference.model._has_forbidden_control_char 对齐：
// 拒绝除 \n / \r / \t 之外的 Unicode Cc 控制字符（含 C0 控制字符与 DEL）。
const UNICODE_CONTROL_CHAR_PATTERN = /\p{Cc}/u;

export function hasForbiddenMultilineControlChar(value: string): boolean {
  for (const character of value) {
    if (
      UNICODE_CONTROL_CHAR_PATTERN.test(character) &&
      character !== "\n" &&
      character !== "\r" &&
      character !== "\t"
    ) {
      return true;
    }
  }
  return false;
}

/**
 * 校验长期工作指令：允许空字符串清空；按 Unicode 码点计数 <= 4000，
 * 并在 trim 前检查非法控制字符，防止 \x0b/\x0c 等控制符在提交前被静默忽略。
 */
export function validateCustomInstructions(value: string): string | null {
  if (hasForbiddenMultilineControlChar(value)) {
    return "长期工作指令不能包含除换行与制表符以外的控制字符。";
  }
  if (countCodePoints(value.trim()) > CUSTOM_INSTRUCTIONS_MAX_LENGTH) {
    return `长期工作指令最多 ${CUSTOM_INSTRUCTIONS_MAX_LENGTH.toLocaleString()} 个字符。`;
  }
  return null;
}

/**
 * 校验显式备忘内容：先拒绝非法控制字符，再校验去首尾空白后为 1–500 个 Unicode 码点。
 */
export function validateUserMemoryContent(value: string): string | null {
  if (hasForbiddenMultilineControlChar(value)) {
    return "备忘内容不能包含除换行与制表符以外的控制字符。";
  }
  const normalized = value.trim();
  const length = countCodePoints(normalized);
  if (length < 1) {
    return "请输入备忘内容。";
  }
  if (length > USER_MEMORY_CONTENT_MAX_LENGTH) {
    return `单条备忘最多 ${USER_MEMORY_CONTENT_MAX_LENGTH} 个字符。`;
  }
  return null;
}

/**
 * 将参考预设填入空草稿，或在现有指令末尾换行追加；若超出 4000 码点上限则拒绝追加并保留原草稿。
 */
export function applyInstructionPreset(
  current: string,
  presetText: string,
): { exceeded: boolean; nextValue: string } {
  const trimmedCurrent = current.trim();
  const candidate = trimmedCurrent
    ? `${current.trimEnd()}\n${presetText}`
    : presetText;
  if (countCodePoints(candidate.trim()) > CUSTOM_INSTRUCTIONS_MAX_LENGTH) {
    return { exceeded: true, nextValue: current };
  }
  return { exceeded: false, nextValue: candidate };
}
