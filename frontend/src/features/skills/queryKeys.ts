import { adminKeys } from "../admin/queryKeys";

export const skillKeys = {
  all: ["skills"] as const,
  catalog: () => ["skills", "catalog"] as const,
  adminList: () => adminKeys.skills(),
  adminDetail: (skillId: string) => adminKeys.skill(skillId),
  adminVersions: (skillId: string) => adminKeys.skillVersionsAll(skillId),
  personalAll: () => ["skills", "personal"] as const,
  personalDetail: (skillId: string) =>
    ["skills", "personal", "detail", skillId] as const,
  personalList: () => ["skills", "personal", "list"] as const,
};
