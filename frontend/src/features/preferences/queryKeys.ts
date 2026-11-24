export const preferenceKeys = {
  all: ["preferences"] as const,
  memories: () => ["preferences", "memories"] as const,
  profile: () => ["preferences", "profile"] as const,
};
