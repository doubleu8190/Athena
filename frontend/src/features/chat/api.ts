import { apiRequest } from "../../shared/api/client";
import type {
  ConversationMessagePage,
  Run,
  RunEventListResponse,
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

export function listRunEvents(
  sessionId: string,
  runId: string,
  afterSequence = 0,
  limit = 100,
): Promise<RunEventListResponse> {
  const params = new URLSearchParams({
    after_sequence: String(afterSequence),
    limit: String(limit),
  });
  return apiRequest<RunEventListResponse>(
    `${runPath(sessionId, runId)}/events?${params.toString()}`,
    { method: "GET" },
  );
}

export interface SubmitRunOptions {
  /**
   * 知识库检索范围三态：
   * - null：企业默认范围（企业公共库 + 本部门共享库，项目内自动合并项目库）
   * - []：本轮显式关闭知识库检索
   * - string[]：仅检索指定的知识库 ID 列表（最多 20 个）
   */
  knowledgeBaseIds?: string[] | null;
  /** 指定对话模型配置 ID；null 表示沿用系统默认模型。 */
  modelProfileId?: string | null;
}

export function submitRun(
  sessionId: string,
  requestId: string,
  text: string,
  skillIds: string[] = [],
  inputFileIds: string[] = [],
  options?: SubmitRunOptions,
): Promise<Run> {
  const body: Record<string, unknown> = {
    request_id: requestId,
    text,
    skill_ids: skillIds,
    input_file_ids: inputFileIds,
  };
  // 显式传入时才写入对应字段，精确区分 undefined（未传入）与 null / [] / string[]。
  if (
    options &&
    "knowledgeBaseIds" in options &&
    options.knowledgeBaseIds !== undefined
  ) {
    body.knowledge_base_ids = options.knowledgeBaseIds;
  }
  if (
    options &&
    "modelProfileId" in options &&
    options.modelProfileId !== undefined
  ) {
    body.model_profile_id = options.modelProfileId;
  }
  return apiRequest<Run>(sessionRunsPath(sessionId), {
    method: "POST",
    body,
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
    body: {
      request_id: requestId,
      confirm_unknown: true,
    },
  });
}

export function answerRunInput(
  sessionId: string,
  runId: string,
  inputRequestId: string,
  requestId: string,
  text: string,
  inputFileIds: string[] = [],
): Promise<Run> {
  return apiRequest<Run>(
    `${runPath(sessionId, runId)}/input-requests/${encodeURIComponent(
      inputRequestId,
    )}/answer`,
    {
      method: "POST",
      body: {
        request_id: requestId,
        text,
        input_file_ids: inputFileIds,
      },
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

