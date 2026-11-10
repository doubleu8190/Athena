export const chatKeys = {
  all: ["chat"] as const,
  latestRun: (sessionId: string) =>
    ["chat", "runs", sessionId, "latest"] as const,
  messages: (sessionId: string) =>
    ["chat", "messages", sessionId] as const,
  run: (sessionId: string, runId: string) =>
    ["chat", "runs", sessionId, "detail", runId] as const,
  runs: (sessionId: string) => ["chat", "runs", sessionId] as const,
  toolCall: (sessionId: string, runId: string, callId: string) =>
    ["chat", "runs", sessionId, "tools", runId, "detail", callId] as const,
  toolCalls: (sessionId: string, runId: string) =>
    ["chat", "runs", sessionId, "tools", runId] as const,
};

