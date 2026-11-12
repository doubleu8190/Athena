export const sessionFileKeys = {
  all: ["session-files"] as const,
  list: (sessionId: string) =>
    ["session-files", sessionId, "list"] as const,
  lists: () => ["session-files"] as const,
};
