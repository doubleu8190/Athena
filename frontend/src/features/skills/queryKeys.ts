import { adminKeys } from "../admin/queryKeys";

export const skillKeys = {
  all: ["skills"] as const,
  catalog: () => ["skills", "catalog"] as const,
  /** 与 catalog 同源的公开技能列表键别名，便于双形态发版后语义化失效。 */
  publicList: () => ["skills", "catalog"] as const,
  adminList: () => adminKeys.skills(),
  adminDetail: (skillId: string) => adminKeys.skill(skillId),
  adminVersions: (skillId: string) => adminKeys.skillVersionsAll(skillId),
  personalAll: () => ["skills", "personal"] as const,
  personalDetail: (skillId: string) =>
    ["skills", "personal", "detail", skillId] as const,
  personalList: () => ["skills", "personal", "list"] as const,
};
