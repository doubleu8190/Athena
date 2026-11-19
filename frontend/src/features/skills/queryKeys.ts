export const skillKeys = {
  all: ["skills"] as const,
  catalog: () => ["skills", "catalog"] as const,
  personalAll: () => ["skills", "personal"] as const,
  personalDetail: (skillId: string) =>
    ["skills", "personal", "detail", skillId] as const,
  personalList: () => ["skills", "personal", "list"] as const,
};
