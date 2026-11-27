import type { ApiSchemas } from "../../shared/api/client";

export const SESSION_TITLE_MAX_LENGTH = 120;

// 直连 OpenAPI 会话契约。
export type Session = ApiSchemas["SessionResponse"];
export type SessionListResponse = ApiSchemas["SessionListResponse"];
export type SessionCreateInput = ApiSchemas["SessionCreateRequest"];
export type SessionUpdateInput = ApiSchemas["SessionUpdateRequest"];

export interface SessionListQuery {
  page?: number;
  pageSize?: number;
  projectId?: string;
  q?: string;
  unbound?: boolean;
}

export type SessionTimeGroupKey =
  | "today"
  | "yesterday"
  | "last7Days"
  | "earlier";

export interface SessionTimeGroup {
  items: Session[];
  key: SessionTimeGroupKey;
  label: string;
}

const SESSION_TIME_GROUP_LABELS: Record<SessionTimeGroupKey, string> = {
  today: "今天",
  yesterday: "昨天",
  last7Days: "近 7 天",
  earlier: "更早",
};

const SESSION_TIME_GROUP_ORDER: readonly SessionTimeGroupKey[] = [
  "today",
  "yesterday",
  "last7Days",
  "earlier",
];

/** 归一化会话搜索词：去首尾空白，空字符串视为未搜索。 */
export function normalizeSessionSearchQuery(value?: string): string {
  return value?.trim() ?? "";
}

/**
 * 按本地日历日期将会话自然分组（今天 / 昨天 / 近 7 天 / 更早），
 * 仅返回非空分组并保持各组内原有倒序排列。
 */
export function groupSessionsByTime(
  sessions: readonly Session[],
  now: Date = new Date(),
): SessionTimeGroup[] {
  const year = now.getFullYear();
  const month = now.getMonth();
  const date = now.getDate();

  // 使用本地日历零点构建分界线，避免夏令时或时区偏移导致跨天误判。
  const todayStart = new Date(year, month, date).getTime();
  const yesterdayStart = new Date(year, month, date - 1).getTime();
  // 近 7 天覆盖含今天在内的最近 7 个自然日（今天、昨天及之前 5 天）。
  const last7DaysStart = new Date(year, month, date - 6).getTime();

  const buckets: Record<SessionTimeGroupKey, Session[]> = {
    today: [],
    yesterday: [],
    last7Days: [],
    earlier: [],
  };

  for (const session of sessions) {
    const updatedAtMs = Date.parse(session.updated_at);
    if (Number.isNaN(updatedAtMs)) {
      buckets.earlier.push(session);
      continue;
    }
    if (updatedAtMs >= todayStart) {
      buckets.today.push(session);
    } else if (updatedAtMs >= yesterdayStart) {
      buckets.yesterday.push(session);
    } else if (updatedAtMs >= last7DaysStart) {
      buckets.last7Days.push(session);
    } else {
      buckets.earlier.push(session);
    }
  }

  return SESSION_TIME_GROUP_ORDER.filter((key) => buckets[key].length > 0).map(
    (key) => ({
      items: buckets[key],
      key,
      label: SESSION_TIME_GROUP_LABELS[key],
    }),
  );
}

const CONTROL_CHARACTER_PATTERN = /\p{Cc}/u;

export function normalizeSessionTitle(value: string): string {
  return value.trim();
}

export function validateSessionTitle(value: string): string | null {
  const normalized = normalizeSessionTitle(value);
  if (!normalized) {
    return "请输入会话标题。";
  }
  if (
    Array.from(normalized).length > SESSION_TITLE_MAX_LENGTH ||
    CONTROL_CHARACTER_PATTERN.test(normalized)
  ) {
    return "会话标题需为 1–120 个字符，且不能包含控制字符。";
  }
  return null;
}

export function sessionDisplayTitle(title: string | null): string {
  return title?.trim() || "未命名对话";
}

