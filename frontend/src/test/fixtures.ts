import type { Project } from "../features/projects/model";
import type { Session } from "../features/sessions/model";
import type { User, UserDepartment } from "../features/auth/model";
import type {
  KnowledgeBase,
  KnowledgeDocument,
  KnowledgeSearchHit,
  KnowledgeVersion,
} from "../features/knowledge/model";
import type {
  ConversationMessage,
  Run,
  RunToolCallDetail,
  RunToolCallSummary,
} from "../features/chat/model";
import type {
  ArtifactFileRef,
  SessionFile,
} from "../features/files/model";

export const DEFAULT_USER_DEPARTMENT: UserDepartment = {
  id: "dept-rd",
  is_primary: true,
  name: "研发部",
  path: "/dept-root/dept-rd/",
};

export function makeUser(overrides: Partial<User> = {}): User {
  return {
    auth_provider: "local",
    avatar_url: null,
    created_at: "2026-11-10T00:00:00Z",
    departments: [DEFAULT_USER_DEPARTMENT],
    display_name: "林知远",
    email: "lin@example.com",
    employee_no: "EMP001",
    id: "user-1",
    job_title: "高级研发工程师",
    managed_department_ids: [],
    role: "member",
    ...overrides,
  };
}

export function makeDeptAdmin(overrides: Partial<User> = {}): User {
  return makeUser({
    departments: [
      {
        id: "dept-fin",
        is_primary: true,
        name: "财务部",
        path: "/dept-root/dept-fin/",
      },
    ],
    display_name: "财务主管",
    email: "fin-admin@example.com",
    employee_no: "EMP002",
    id: "dept-admin-1",
    job_title: "财务部负责人",
    managed_department_ids: ["dept-fin"],
    role: "dept_admin",
    ...overrides,
  });
}

export function makeSuperAdmin(overrides: Partial<User> = {}): User {
  return makeUser({
    created_at: "2026-11-01T00:00:00Z",
    departments: [
      {
        id: "dept-root",
        is_primary: true,
        name: "集团总部",
        path: "/dept-root/",
      },
    ],
    display_name: "系统超级管理员",
    email: "admin@example.com",
    employee_no: "EMP000",
    id: "admin-1",
    job_title: "企业信息安全负责人",
    managed_department_ids: [],
    role: "super_admin",
    ...overrides,
  });
}

export const TEST_USER: User = makeUser();

export const PROJECT_A: Project = {
  created_at: "2026-11-08T08:00:00Z",
  default_skill_ids: [],
  description: null,
  id: "project-a",
  instructions: null,
  name: "增长战略",
  updated_at: "2026-11-10T02:00:00Z",
};

export const PROJECT_B: Project = {
  created_at: "2026-11-09T09:00:00Z",
  default_skill_ids: [],
  description: null,
  id: "project-b",
  instructions: null,
  name: "产品研究",
  updated_at: "2026-11-10T01:00:00Z",
};

export const KNOWLEDGE_BASE_A: KnowledgeBase = {
  created_at: "2026-11-08T08:00:00Z",
  description: "产品访谈与竞品资料",
  id: "knowledge-a",
  name: "产品研究库",
  owner_department_id: null,
  updated_at: "2026-11-10T04:00:00Z",
  visibility_grants: [],
  visibility_scope: "personal",
};

export const KNOWLEDGE_BASE_B: KnowledgeBase = {
  created_at: "2026-11-09T09:00:00Z",
  description: null,
  id: "knowledge-b",
  name: "市场资料库",
  owner_department_id: null,
  updated_at: "2026-11-10T05:00:00Z",
  visibility_grants: [],
  visibility_scope: "personal",
};

export function makeKnowledgeDocument(
  overrides: Partial<KnowledgeDocument> & Pick<KnowledgeDocument, "id">,
): KnowledgeDocument {
  const { id, ...rest } = overrides;
  const activeVersionId =
    rest.active_version_id !== undefined ? rest.active_version_id : "version-1";
  const latestVersion =
    rest.latest_version !== undefined
      ? rest.latest_version
      : activeVersionId !== null
        ? makeKnowledgeVersion({
            document_id: id,
            id: activeVersionId,
          })
        : null;
  return {
    active_version_id: activeVersionId,
    created_at: "2026-11-09T08:00:00Z",
    id,
    knowledge_base_id: KNOWLEDGE_BASE_A.id,
    latest_version: latestVersion,
    pending_version_id: null,
    title: "真实知识文档",
    updated_at: "2026-11-10T04:30:00Z",
    ...rest,
  };
}

export function makeKnowledgeVersion(
  overrides: Partial<KnowledgeVersion> &
    Pick<KnowledgeVersion, "id" | "document_id">,
): KnowledgeVersion {
  const { document_id, id, ...rest } = overrides;
  return {
    chunk_count: 8,
    completed_at: "2026-11-10T04:31:00Z",
    created_at: "2026-11-10T04:30:00Z",
    document_id,
    error_code: null,
    id,
    state: "ready",
    warnings: [],
    ...rest,
  };
}

export function makeKnowledgeHit(
  overrides: Partial<KnowledgeSearchHit> &
    Pick<KnowledgeSearchHit, "chunk_id">,
): KnowledgeSearchHit {
  const { chunk_id, ...rest } = overrides;
  return {
    channels: ["keyword", "semantic"],
    chunk_id,
    chunk_ids: [chunk_id],
    document_id: "document-1",
    locators: [{ kind: "pdf", page: 3 }],
    text: "真实检索片段内容",
    title: "真实知识文档",
    version_id: "version-1",
    ...rest,
  };
}

export function makeSession(
  overrides: Partial<Session> & Pick<Session, "id">,
): Session {
  return {
    created_at: "2026-11-09T08:00:00Z",
    project_id: PROJECT_A.id,
    title: "真实会话",
    updated_at: "2026-11-10T03:00:00Z",
    ...overrides,
  };
}

export function makeSessionFile(
  overrides: Partial<SessionFile> & Pick<SessionFile, "id">,
): SessionFile {
  const { id, ...rest } = overrides;
  return {
    created_at: "2026-11-12T02:00:00Z",
    extracted_text_bytes: null,
    extraction_error_code: null,
    extraction_status: "none",
    filename: "sales.csv",
    id,
    label: null,
    mime_type: "text/csv",
    session_id: "session-1",
    size_bytes: 1024,
    source: "upload",
    ...rest,
  };
}

export function makeArtifact(
  overrides: Partial<ArtifactFileRef> &
    Pick<ArtifactFileRef, "file_id">,
): ArtifactFileRef {
  const { file_id, ...rest } = overrides;
  return {
    file_id,
    filename: "report.xlsx",
    label: "月度销售报表",
    mime_type:
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    size_bytes: 20_480,
    ...rest,
  };
}

export function makeMessage(
  overrides: Partial<ConversationMessage> &
    Pick<ConversationMessage, "id" | "sequence" | "session_id">,
): ConversationMessage {
  const { id, sequence, session_id, ...rest } = overrides;
  const text = overrides.parts?.[0]?.type === "text"
    ? overrides.parts[0].text
    : "消息内容";
  return {
    completed_at: "2026-11-10T03:00:00Z",
    created_at: "2026-11-10T03:00:00Z",
    id,
    parts: [{ text, type: "text" }],
    reply_to_message_id: null,
    role: "assistant",
    run_id: "run-1",
    sequence,
    session_id,
    status: "completed",
    ...rest,
  };
}

export function makeRun(
  overrides: Partial<Run> & Pick<Run, "id" | "session_id" | "status">,
): Run {
  const { id, session_id, status, ...rest } = overrides;
  return {
    assistant_message_id: null,
    cancel_requested_at: null,
    context_cutoff_sequence: 0,
    created_at: "2026-11-10T03:00:00Z",
    depth: 0,
    event_sequence: 1,
    execution_epoch: 1,
    finished_at: null,
    first_started_at: null,
    id,
    input_file_ids: [],
    input_interactions: [],
    knowledge_base_ids: null,
    model_config_version_id: "model-config-1",
    model_snapshot: {
      id: "model-config-1",
      model: "atlas-main",
      name: "主力模型",
      scenario_label: null,
      supports_vision: false,
      version: 1,
    },
    parent_run_id: null,
    parent_tool_call_id: null,
    pending_approvals: [],
    pending_input: null,
    pending_subagent_approvals: [],
    pending_subagent_inputs: [],
    prompt_template_version: "run_prompt_v1",
    queued_at: "2026-11-10T03:00:00Z",
    replayed: false,
    retry_of_run_id: null,
    retry_warning: null,
    report_message_id: null,
    root_run_id: null,
    safe_error_code: null,
    session_id,
    skill_snapshot: null,
    status,
    subagent_name: null,
    subagent_profile_id: null,
    subagent_slug: null,
    subagent_tasks: [],
    trigger_message_id: "msg-user-1",
    updated_at: "2026-11-10T03:00:00Z",
    usage: {
      answer_tokens: null,
      attempts: [],
      complete: false,
      compression_tokens: null,
      input_tokens: null,
      output_tokens: null,
      total_tokens: null,
    },
    ...rest,
  };
}

export function makeToolCall(
  overrides: Partial<RunToolCallSummary> &
    Pick<RunToolCallSummary, "id" | "ordinal" | "tool_call_id">,
): RunToolCallSummary {
  const { id, ordinal, tool_call_id, ...rest } = overrides;
  return {
    display_name: null,
    duration_ms: 120,
    finished_at: "2026-11-10T03:00:01Z",
    id,
    model_name: "web_search",
    ordinal,
    safe_error_code: null,
    started_at: "2026-11-10T03:00:00Z",
    status: "completed",
    tool_id: "tool-1",
    tool_kind: "mcp",
    tool_call_id,
    truncated: false,
    ...rest,
  };
}

export function makeToolCallDetail(
  overrides: Partial<RunToolCallDetail> &
    Pick<RunToolCallDetail, "id" | "ordinal" | "tool_call_id">,
): RunToolCallDetail {
  return {
    ...makeToolCall(overrides),
    artifacts: [],
    business_summary: null,
    formatted_arguments: [
      { key: "query", label: "查询条件", value: "真实参数" },
    ],
    hidden_argument_count: 0,
    outcome: {
      formatted_results: [
        { key: "result", label: "查询结果", value: "真实结果" },
      ],
      status: "ok",
      summary: "工具执行成功",
      truncated: false,
    },
    safe_skill_projection: null,
    ...overrides,
  };
}

export function getRequestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") {
    return input;
  }
  if (input instanceof URL) {
    return input.toString();
  }
  return input.url;
}

export function listResponse<T>(
  items: T[],
  page = 1,
  pageSize = 20,
  total = items.length,
) {
  return {
    items,
    page,
    page_size: pageSize,
    total,
  };
}

export function knowledgeListResponse<T>(
  items: T[],
  page = 1,
  pageSize = 20,
) {
  return {
    items,
    page,
    page_size: pageSize,
  };
}

