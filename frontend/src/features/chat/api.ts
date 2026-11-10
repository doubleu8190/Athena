import { apiRequest } from "../../shared/api/client";
import type {
  ConversationMessagePage,
  Run,
  RunListResponse,
  RunToolCallDetail,
  RunToolCallListResponse,
} from "./model";

const API_PREFIX = "/api/v1";

function sessionRunsPath(sessionId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/runs`;
}

function runPath(sessionId: string, runId: string): string {
  return `${sessionRunsPath(sessionId)}/${encodeURIComponent(runId)}`;
}

export function listMessages(
  sessionId: string,
  beforeSequence?: number,
  limit = 20,
): Promise<ConversationMessagePage> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (beforeSequence !== undefined) {
    params.set("before_sequence", String(beforeSequence));
  }
  return apiRequest<ConversationMessagePage>(
    `/sessions/${encodeURIComponent(sessionId)}/messages?${params.toString()}`,
    { method: "GET" },
  );
}

export function listRuns(
  sessionId: string,
  limit = 1,
): Promise<RunListResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  return apiRequest<RunListResponse>(
    `${sessionRunsPath(sessionId)}?${params.toString()}`,
    { method: "GET" },
  );
}

export function getRun(sessionId: string, runId: string): Promise<Run> {
  return apiRequest<Run>(runPath(sessionId, runId), { method: "GET" });
}

export function submitRun(
  sessionId: string,
  requestId: string,
  text: string,
): Promise<Run> {
  return apiRequest<Run>(sessionRunsPath(sessionId), {
    method: "POST",
    body: { request_id: requestId, text },
  });
}

export function cancelRun(sessionId: string, runId: string): Promise<Run> {
  return apiRequest<Run>(`${runPath(sessionId, runId)}/cancel`, {
    method: "POST",
  });
}

export function retryRun(
  sessionId: string,
  runId: string,
  requestId: string,
): Promise<Run> {
  return apiRequest<Run>(`${runPath(sessionId, runId)}/retry`, {
    method: "POST",
    body: { request_id: requestId, confirm_unknown: true },
  });
}

export function answerRunInput(
  sessionId: string,
  runId: string,
  inputRequestId: string,
  requestId: string,
  text: string,
): Promise<Run> {
  return apiRequest<Run>(
    `${runPath(sessionId, runId)}/input-requests/${encodeURIComponent(
      inputRequestId,
    )}/answer`,
    {
      method: "POST",
      body: { request_id: requestId, text },
    },
  );
}

export function decideRunToolApproval(
  sessionId: string,
  runId: string,
  callId: string,
  requestId: string,
  decision: "approve" | "reject",
): Promise<Run> {
  return apiRequest<Run>(
    `${runPath(sessionId, runId)}/tool-calls/${encodeURIComponent(
      callId,
    )}/approval`,
    {
      method: "POST",
      body: { request_id: requestId, decision },
    },
  );
}

export function listRunToolCalls(
  sessionId: string,
  runId: string,
  afterOrdinal = 0,
  limit = 100,
): Promise<RunToolCallListResponse> {
  const params = new URLSearchParams({
    after_ordinal: String(afterOrdinal),
    limit: String(limit),
  });
  return apiRequest<RunToolCallListResponse>(
    `${runPath(sessionId, runId)}/tool-calls?${params.toString()}`,
    { method: "GET" },
  );
}

export function getRunToolCall(
  sessionId: string,
  runId: string,
  callId: string,
): Promise<RunToolCallDetail> {
  return apiRequest<RunToolCallDetail>(
    `${runPath(sessionId, runId)}/tool-calls/${encodeURIComponent(callId)}`,
    { method: "GET" },
  );
}

export function runStreamUrl(sessionId: string, runId: string): string {
  return `${API_PREFIX}${runPath(sessionId, runId)}/stream`;
}

