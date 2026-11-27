import { apiGet, apiPost, type ApiSchemas } from "../../shared/api/client";
import type { SkillScope } from "../skills/model";
import type {
  ConversationMessagePage,
  Run,
  RunListResponse,
  RunSkillSnapshot,
  RunStatus,
  RunToolCallDetail,
  RunToolCallListResponse,
  SubagentTaskSummary,
} from "./model";

const API_PREFIX = "/api/v1";

function toRunStatus(value: string): RunStatus {
  switch (value) {
    case "queued":
    case "running":
    case "waiting_for_input":
    case "waiting_for_approval":
    case "waiting_for_subagents":
    case "cancelling":
    case "completed":
    case "failed":
    case "cancelled":
      return value;
    default:
      return "failed";
  }
}

function toSkillScope(value: string): SkillScope {
  if (
    value === "company" ||
    value === "department" ||
    value === "personal"
  ) {
    return value;
  }
  return "company";
}

function toRunSkillSnapshot(
  raw: ApiSchemas["RunSkillSnapshotResponse"],
): RunSkillSnapshot {
  return {
    ...raw,
    skills: raw.skills.map((entry) => ({
      ...entry,
      scope: toSkillScope(entry.scope),
    })),
  };
}

function toSubagentTaskSummary(
  raw: ApiSchemas["SubagentTaskResponse"],
): SubagentTaskSummary {
  return {
    ...raw,
    status: toRunStatus(raw.status),
  };
}

function toRun(raw: ApiSchemas["RunResponse"]): Run {
  return {
    ...raw,
    skill_snapshot: raw.skill_snapshot
      ? toRunSkillSnapshot(raw.skill_snapshot)
      : null,
    status: toRunStatus(raw.status),
    subagent_tasks: raw.subagent_tasks.map(toSubagentTaskSummary),
  };
}

export function listMessages(
  sessionId: string,
  beforeSequence?: number,
  limit = 20,
): Promise<ConversationMessagePage> {
  const query: { limit: number; before_sequence?: number } = { limit };
  if (beforeSequence !== undefined) {
    query.before_sequence = beforeSequence;
  }
  return apiGet("/api/v1/sessions/{session_id}/messages", {
    params: {
      path: { session_id: sessionId },
      query,
    },
  });
}

export async function listRuns(
  sessionId: string,
  limit = 1,
): Promise<RunListResponse> {
  const response = await apiGet("/api/v1/sessions/{session_id}/runs", {
    params: {
      path: { session_id: sessionId },
      query: { limit },
    },
  });
  return {
    items: response.items.map(toRun),
    next_cursor: response.next_cursor,
  };
}

export async function getRun(sessionId: string, runId: string): Promise<Run> {
  return toRun(
    await apiGet("/api/v1/sessions/{session_id}/runs/{run_id}", {
      params: { path: { run_id: runId, session_id: sessionId } },
    }),
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

export async function submitRun(
  sessionId: string,
  requestId: string,
  text: string,
  skillIds: string[] = [],
  inputFileIds: string[] = [],
  options?: SubmitRunOptions,
): Promise<Run> {
  const body: ApiSchemas["RunSubmitRequest"] = {
    request_id: requestId,
    text,
    skill_ids: skillIds,
    input_file_ids: inputFileIds,
  };
  // 显式传入时才写入对应字段，精确区分 undefined（未传入）与 null / [] / string[] 三态。
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
  return toRun(
    await apiPost("/api/v1/sessions/{session_id}/runs", {
      body,
      params: { path: { session_id: sessionId } },
    }),
  );
}

export async function cancelRun(
  sessionId: string,
  runId: string,
): Promise<Run> {
  return toRun(
    await apiPost("/api/v1/sessions/{session_id}/runs/{run_id}/cancel", {
      params: { path: { run_id: runId, session_id: sessionId } },
    }),
  );
}

export async function retryRun(
  sessionId: string,
  runId: string,
  requestId: string,
): Promise<Run> {
  return toRun(
    await apiPost("/api/v1/sessions/{session_id}/runs/{run_id}/retry", {
      body: {
        confirm_unknown: true,
        request_id: requestId,
      },
      params: { path: { run_id: runId, session_id: sessionId } },
    }),
  );
}

export async function answerRunInput(
  sessionId: string,
  runId: string,
  inputRequestId: string,
  requestId: string,
  text: string,
  inputFileIds: string[] = [],
): Promise<Run> {
  return toRun(
    await apiPost(
      "/api/v1/sessions/{session_id}/runs/{run_id}/input-requests/{input_request_id}/answer",
      {
        body: {
          input_file_ids: inputFileIds,
          request_id: requestId,
          text,
        },
        params: {
          path: {
            input_request_id: inputRequestId,
            run_id: runId,
            session_id: sessionId,
          },
        },
      },
    ),
  );
}

export async function decideRunToolApproval(
  sessionId: string,
  runId: string,
  callId: string,
  requestId: string,
  decision: "approve" | "reject",
): Promise<Run> {
  return toRun(
    await apiPost(
      "/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/{call_id}/approval",
      {
        body: { decision, request_id: requestId },
        params: {
          path: { call_id: callId, run_id: runId, session_id: sessionId },
        },
      },
    ),
  );
}

export function listRunToolCalls(
  sessionId: string,
  runId: string,
  afterOrdinal = 0,
  limit = 100,
): Promise<RunToolCallListResponse> {
  return apiGet("/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls", {
    params: {
      path: { run_id: runId, session_id: sessionId },
      query: { after_ordinal: afterOrdinal, limit },
    },
  });
}

export function getRunToolCall(
  sessionId: string,
  runId: string,
  callId: string,
): Promise<RunToolCallDetail> {
  return apiGet(
    "/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/{call_id}",
    {
      params: {
        path: { call_id: callId, run_id: runId, session_id: sessionId },
      },
    },
  );
}

export function runStreamUrl(sessionId: string, runId: string): string {
  return `${API_PREFIX}/sessions/${encodeURIComponent(
    sessionId,
  )}/runs/${encodeURIComponent(runId)}/stream`;
}
