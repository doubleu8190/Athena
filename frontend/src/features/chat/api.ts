import { apiGet, apiPost, type ApiSchemas } from "../../shared/api/client";
import type { ArtifactFileRef } from "../files/model";
import type { LegacyCompatibleSkillScope } from "../skills/model";
import type {
  ConversationMessage,
  ConversationMessagePage,
  ConversationMessagePart,
  PendingSubagentApproval,
  PendingSubagentInput,
  Run,
  RunEventListResponse,
  RunListResponse,
  RunModelSnapshot,
  RunPendingApproval,
  RunSkillSnapshot,
  RunStatus,
  RunToolCallDetail,
  RunToolCallListResponse,
  RunUsage,
  RunUsageAttempt,
  SafeSkillApprovalProjection,
  SafeSkillInputFile,
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

function toSkillScope(value: string): LegacyCompatibleSkillScope {
  if (
    value === "company" ||
    value === "department" ||
    value === "personal" ||
    value === "public"
  ) {
    return value;
  }
  return "company";
}

function toArtifactFileRef(
  raw: ApiSchemas["ArtifactFileRefResponse"],
): ArtifactFileRef {
  return {
    ...raw,
    label: raw.label ?? null,
  };
}

function toSafeSkillInputFile(
  raw: ApiSchemas["SafeSkillInputFileResponse"],
): SafeSkillInputFile {
  return {
    ...raw,
    mime_type: raw.mime_type ?? null,
    size_bytes: raw.size_bytes ?? null,
  };
}

function toSafeSkillApprovalProjection(
  raw: ApiSchemas["SafeSkillApprovalProjectionResponse"],
): SafeSkillApprovalProjection {
  return {
    ...raw,
    can_approve: raw.can_approve ?? true,
    hidden_parameter_count: raw.hidden_parameter_count ?? 0,
    input_files: (raw.input_files ?? []).map(toSafeSkillInputFile),
    parameter_summary: raw.parameter_summary ?? [],
    unavailable_reason: raw.unavailable_reason ?? null,
  };
}

function toMessagePart(
  raw: ApiSchemas["MessagePartResponse"],
): ConversationMessagePart {
  if (raw.type === "file_ref") {
    return {
      ...raw,
      mime_type: raw.mime_type ?? null,
      size_bytes: raw.size_bytes ?? null,
    };
  }
  if (raw.type === "tool_result") {
    return {
      ...raw,
      artifact_id: raw.artifact_id ?? null,
      artifacts: (raw.artifacts ?? []).map(toArtifactFileRef),
      output: raw.output ?? null,
    };
  }
  return raw;
}

function toConversationMessage(
  raw: ApiSchemas["ConversationMessageResponse"],
): ConversationMessage {
  return {
    ...raw,
    parts: (raw.parts ?? []).map(toMessagePart),
  };
}

function toRunUsageAttempt(
  raw: ApiSchemas["RunAttemptUsageResponse"],
): RunUsageAttempt {
  return {
    ...raw,
    batch_index: raw.batch_index ?? 0,
    duration_ms: raw.duration_ms ?? null,
    model_profile_id: raw.model_profile_id ?? null,
    operation_id: raw.operation_id ?? null,
    purpose: raw.purpose ?? "answer",
  };
}

function toRunUsage(raw: ApiSchemas["RunUsageResponse"]): RunUsage {
  return {
    ...raw,
    answer_tokens: raw.answer_tokens ?? null,
    attempts: (raw.attempts ?? []).map(toRunUsageAttempt),
    compression_tokens: raw.compression_tokens ?? null,
  };
}

function toRunPendingApproval(
  raw: ApiSchemas["RunPendingToolApprovalResponse"],
): RunPendingApproval {
  return {
    ...raw,
    safe_skill_projection: raw.safe_skill_projection
      ? toSafeSkillApprovalProjection(raw.safe_skill_projection)
      : null,
  };
}

function toRunSkillSnapshot(
  raw: ApiSchemas["RunSkillSnapshotResponse"],
): RunSkillSnapshot {
  return {
    ...raw,
    skills: (raw.skills ?? []).map((entry) => ({
      ...entry,
      scope: toSkillScope(entry.scope),
    })),
  };
}

function toRunModelSnapshot(
  raw: ApiSchemas["RunModelSnapshotResponse"],
): RunModelSnapshot {
  return {
    ...raw,
    scenario_label: raw.scenario_label ?? null,
    supports_vision: raw.supports_vision ?? false,
    version: raw.version ?? 1,
  };
}

function toSubagentTaskSummary(
  raw: ApiSchemas["SubagentTaskResponse"],
): SubagentTaskSummary {
  return {
    ...raw,
    finished_at: raw.finished_at ?? null,
    first_started_at: raw.first_started_at ?? null,
    safe_error_code: raw.safe_error_code ?? null,
    status: toRunStatus(raw.status),
    subagent_profile_id: raw.subagent_profile_id ?? null,
    task_index: raw.task_index ?? 1,
    task_summary: raw.task_summary ?? "",
  };
}

function toPendingSubagentInput(
  raw: ApiSchemas["PendingSubagentInputResponse"],
): PendingSubagentInput {
  return {
    ...raw,
    task_index: raw.task_index ?? 1,
    task_summary: raw.task_summary ?? "",
  };
}

function toPendingSubagentApproval(
  raw: ApiSchemas["PendingSubagentApprovalResponse"],
): PendingSubagentApproval {
  return {
    ...raw,
    safe_skill_projection: raw.safe_skill_projection
      ? toSafeSkillApprovalProjection(raw.safe_skill_projection)
      : null,
    task_index: raw.task_index ?? 1,
    task_summary: raw.task_summary ?? "",
    tool_kind: raw.tool_kind ?? null,
  };
}

function toRun(raw: ApiSchemas["RunResponse"]): Run {
  const {
    input_interactions,
    model_snapshot,
    pending_subagent_approvals,
    pending_subagent_inputs,
    subagent_tasks,
    ...rest
  } = raw;
  const normalized: Run = {
    ...rest,
    pending_approvals: (raw.pending_approvals ?? []).map(toRunPendingApproval),
    pending_input: raw.pending_input ?? null,
    replayed: raw.replayed ?? false,
    retry_warning: raw.retry_warning ?? null,
    skill_snapshot: raw.skill_snapshot
      ? toRunSkillSnapshot(raw.skill_snapshot)
      : null,
    status: toRunStatus(raw.status),
    usage: toRunUsage(raw.usage),
  };
  if (model_snapshot !== undefined) {
    normalized.model_snapshot =
      model_snapshot === null ? null : toRunModelSnapshot(model_snapshot);
  }
  if (input_interactions !== undefined) {
    normalized.input_interactions = input_interactions.map((interaction) => ({
      ...interaction,
      answer_text: interaction.answer_text ?? null,
      answered_at: interaction.answered_at ?? null,
    }));
  }
  if (subagent_tasks !== undefined) {
    normalized.subagent_tasks = subagent_tasks.map(toSubagentTaskSummary);
  }
  if (pending_subagent_inputs !== undefined) {
    normalized.pending_subagent_inputs = pending_subagent_inputs.map(
      toPendingSubagentInput,
    );
  }
  if (pending_subagent_approvals !== undefined) {
    normalized.pending_subagent_approvals = pending_subagent_approvals.map(
      toPendingSubagentApproval,
    );
  }
  return normalized;
}

function toRunToolCallDetail(
  raw: ApiSchemas["RunToolCallDetailResponse"],
): RunToolCallDetail {
  return {
    ...raw,
    arguments: raw.arguments ?? null,
    artifacts: (raw.artifacts ?? []).map(toArtifactFileRef),
    outcome: raw.outcome
      ? {
          ...raw.outcome,
          cleanup_error_code: raw.outcome.cleanup_error_code ?? null,
        }
      : null,
    safe_skill_projection: raw.safe_skill_projection
      ? toSafeSkillApprovalProjection(raw.safe_skill_projection)
      : null,
  };
}

export async function listMessages(
  sessionId: string,
  beforeSequence?: number,
  limit = 20,
): Promise<ConversationMessagePage> {
  const query: { limit: number; before_sequence?: number } = { limit };
  if (beforeSequence !== undefined) {
    query.before_sequence = beforeSequence;
  }
  const response = await apiGet("/api/v1/sessions/{session_id}/messages", {
    params: {
      path: { session_id: sessionId },
      query,
    },
  });
  return {
    items: response.items.map(toConversationMessage),
    next_before_sequence: response.next_before_sequence,
  };
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

export function listRunEvents(
  sessionId: string,
  runId: string,
  afterSequence = 0,
  limit = 100,
): Promise<RunEventListResponse> {
  return apiGet("/api/v1/sessions/{session_id}/runs/{run_id}/events", {
    params: {
      path: { run_id: runId, session_id: sessionId },
      query: { after_sequence: afterSequence, limit },
    },
  });
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

export async function getRunToolCall(
  sessionId: string,
  runId: string,
  callId: string,
): Promise<RunToolCallDetail> {
  return toRunToolCallDetail(
    await apiGet(
      "/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/{call_id}",
      {
        params: {
          path: { call_id: callId, run_id: runId, session_id: sessionId },
        },
      },
    ),
  );
}

export function runStreamUrl(sessionId: string, runId: string): string {
  return `${API_PREFIX}/sessions/${encodeURIComponent(
    sessionId,
  )}/runs/${encodeURIComponent(runId)}/stream`;
}
