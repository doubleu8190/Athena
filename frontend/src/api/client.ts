/**
 * REST API 客户端，封装与后端 FastAPI 的 HTTP 通信。
 */
import type {
  CreateSessionResponse,
  Session,
  Message,
  ApprovalRequest,
  ApprovalLog,
  ApprovalStats,
  MemoryEntry,
  MemoryListResponse,
  SessionRunSummary,
  RunTimelineResponse,
  RunOrchestrationSnapshot,
  PlanVersionDetail,
  ProviderInfo,
  SettingsView,
  TestProviderResult,
  ToolListResponse,
  McpRegisterPayload,
  McpRegisterResponse,
  McpServerListResponse,
  Attachment,
  SupportedAttachmentTypes,
  KnowledgeBase,
  RetrievalRunDetail,
  RetrievalRunListResponse,
  EvaluationRunSummary,
  EvaluationRunDetail,
} from "../types"

export interface AuthSession {
  authenticated: boolean
  username?: string | null
}

export interface LoginResponse extends AuthSession {
  status: string
}

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

class ApiClient {
  private baseUrl = ""
  private unauthorizedHandler: (() => void) | null = null

  /** 设置 API 根地址；会移除末尾斜杠以避免拼接出双斜杠。 */
  setBaseUrl(url: string): void {
    this.baseUrl = url.replace(/\/$/, "")
  }

  /** 返回当前 API 根地址。 */
  getBaseUrl(): string {
    return this.baseUrl
  }

  /** 注册 401 回调，让应用回到登录页。 */
  setUnauthorizedHandler(handler: (() => void) | null): void {
    this.unauthorizedHandler = handler
  }

  /**
   * 执行 JSON HTTP 请求并统一处理状态码。
   * @param path 相对 API 路径，必须以 `/` 开头。
   * @param options fetch 请求选项；默认使用 JSON 请求头。
   * @returns Promise<T> 后端响应体；204 响应返回 undefined。
   * @throws Error 当网络失败、响应非 2xx 或响应体无法解析时抛出。
   */
  private async request<T>(
    path: string,
    options: RequestInit = {},
  ): Promise<T> {
    const headers = new Headers(options.headers)
    if (!(options.body instanceof FormData) && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json")
    }
    const url = `${this.baseUrl}${path}`
    const res = await fetch(url, {
      headers,
      ...options,
      credentials: "include",
    })
    if (!res.ok) {
      if (res.status === 401) this.unauthorizedHandler?.()
      const errorText = await res.text().catch(() => res.statusText)
      let detail = errorText
      try {
        const parsed = JSON.parse(errorText) as { detail?: unknown }
        if (parsed.detail != null) detail = String(parsed.detail)
      } catch {
        // Keep the original response body when it is not JSON.
      }
      throw new ApiError(res.status, `API ${res.status}: ${detail || res.statusText}`)
    }
    if (res.status === 204) return undefined as T
    return res.json()
  }

  // ─── 健康检查 ─────────────────────────────────────────────────

  /** 获取后端健康状态和版本信息。 */
  async healthCheck(): Promise<{ status: string; timestamp: string }> {
    return this.request<{ status: string; timestamp: string }>("/api/health")
  }

  /** 查询当前 Cookie 会话状态。 */
  async getAuthSession(): Promise<AuthSession> {
    return this.request<AuthSession>("/api/auth/session")
  }

  /** 使用配置账号登录并写入 HttpOnly Cookie。 */
  async login(username: string, password: string): Promise<LoginResponse> {
    return this.request<LoginResponse>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    })
  }

  /** 注销当前 Cookie 会话。 */
  async logout(): Promise<{ status: string }> {
    return this.request<{ status: string }>("/api/auth/logout", { method: "POST" })
  }

  // ─── 会话管理 ─────────────────────────────────────────────────

  /** 查询全部会话，后端负责按最近更新时间排序。 */
  async listSessions(): Promise<Session[]> {
    return this.request<Session[]>("/api/sessions")
  }

  /** 创建会话。
   * @param title 会话标题；为空时使用默认标题。
   */
  async createSession(title: string = "New Session"): Promise<CreateSessionResponse> {
    return this.request<CreateSessionResponse>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ title }),
    })
  }

  /** 按 ID 查询会话详情。 */
  async getSession(sessionId: string): Promise<Session> {
    return this.request<Session>(`/api/sessions/${sessionId}`)
  }

  /** 删除会话及其关联资源。 */
  async deleteSession(sessionId: string): Promise<{ status: string }> {
    return this.request<{ status: string }>(`/api/sessions/${sessionId}`, {
      method: "DELETE",
    })
  }

  /** 查询会话消息，可选限制返回条数。 */
  async getMessages(sessionId: string, limit?: number): Promise<Message[]> {
    const params = limit ? `?limit=${limit}` : ""
    return this.request<Message[]>(`/api/sessions/${sessionId}/messages${params}`)
  }

  async listSessionRuns(sessionId: string): Promise<SessionRunSummary[]> {
    return this.request<SessionRunSummary[]>(`/api/sessions/${encodeURIComponent(sessionId)}/runs`)
  }

  async getRunTimeline(sessionId: string, runId: string, after = 0, limit = 200): Promise<RunTimelineResponse> {
    const params = new URLSearchParams({ after: String(after), limit: String(limit) })
    return this.request<RunTimelineResponse>(
      `/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/timeline?${params.toString()}`,
    )
  }

  async getRunOrchestration(runId: string): Promise<RunOrchestrationSnapshot> {
    return this.request<RunOrchestrationSnapshot>(
      `/api/runs/${encodeURIComponent(runId)}/orchestration`,
    )
  }

  async getPlanVersion(planId: string, version: number): Promise<PlanVersionDetail> {
    return this.request<PlanVersionDetail>(
      `/api/plans/${encodeURIComponent(planId)}/versions/${version}`,
    )
  }

  /** 分页读取召回运行摘要，可按范围、状态、会话或查询文本过滤。 */
  async listRetrievalRuns(opts?: {
    limit?: number
    offset?: number
    scope?: string
    status?: string
    session_id?: string
    agent_run_id?: string
    q?: string
  }): Promise<RetrievalRunListResponse> {
    const params = new URLSearchParams()
    if (opts?.limit != null) params.set("limit", String(opts.limit))
    if (opts?.offset != null) params.set("offset", String(opts.offset))
    if (opts?.scope) params.set("scope", opts.scope)
    if (opts?.status) params.set("status", opts.status)
    if (opts?.session_id) params.set("session_id", opts.session_id)
    if (opts?.agent_run_id) params.set("agent_run_id", opts.agent_run_id)
    if (opts?.q) params.set("q", opts.q)
    const query = params.toString()
    return this.request<RetrievalRunListResponse>(
      `/api/retrieval/runs${query ? `?${query}` : ""}`,
    )
  }

  /** 读取一次召回运行的配置、阶段候选和上下文注入状态。 */
  async getRetrievalRun(runId: string): Promise<RetrievalRunDetail> {
    return this.request<RetrievalRunDetail>(
      `/api/retrieval/runs/${encodeURIComponent(runId)}`,
    )
  }

  async listEvaluationRuns(opts?: { limit?: number; offset?: number; status?: string; dataset_version?: string }): Promise<{ items: EvaluationRunSummary[]; total: number; limit: number; offset: number }> {
    const params = new URLSearchParams()
    if (opts?.limit != null) params.set("limit", String(opts.limit))
    if (opts?.offset != null) params.set("offset", String(opts.offset))
    if (opts?.status) params.set("status", opts.status)
    if (opts?.dataset_version) params.set("dataset_version", opts.dataset_version)
    return this.request(`/api/evaluations/runs${params.size ? `?${params}` : ""}`)
  }

  async getEvaluationRun(runId: string): Promise<EvaluationRunDetail> {
    return this.request(`/api/evaluations/runs/${encodeURIComponent(runId)}`)
  }

  async validateEvaluationDataset(path: string, name?: string): Promise<{ dataset_name: string; version_id: string; manifest_sha256: string; case_count: number; tag_counts: Record<string, number> }> {
    return this.request("/api/evaluations/datasets/validate", { method: "POST", body: JSON.stringify({ path, name }) })
  }

  async startEvaluationRun(payload: { dataset_path: string; name?: string; mode?: string; target?: Record<string, unknown>; evaluators?: string[]; gates?: Array<Record<string, unknown>> }): Promise<{ run_id: string; status: string; case_count: number }> {
    return this.request("/api/evaluations/runs", { method: "POST", body: JSON.stringify(payload) })
  }

  getEvaluationEventsUrl(runId: string): string {
    return `${this.baseUrl}/api/evaluations/runs/${encodeURIComponent(runId)}/events`
  }

  /** 提交异步 Agent 运行命令。
   * @param sessionId 目标会话 ID，必须非空。
   * @param payload 用户消息和可选附件/幂等命令 ID。
   * @returns Promise 包含命令 ID、运行 ID及排队状态。
   * @throws Error 当会话不存在、命令冲突或会话不可并行运行时抛出。
   */
  async submitRun(sessionId: string, payload: { message: string; attachment_ids?: string[]; command_id?: string }): Promise<{ command_id: string; run_id?: string; message_id: string; attachment_ids: string[]; status: string; deduplicated?: boolean }> {
    const commandId = payload.command_id || `cmd_${crypto.randomUUID()}`
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/runs`, {
      method: "POST",
      body: JSON.stringify({
        message: payload.message,
        command_id: commandId,
        attachment_ids: payload.attachment_ids ?? [],
      }),
    })
  }

  /** 提交取消运行命令。
   * @param runId 可选目标运行 ID；为空时由后端选择当前运行。
   */
  async cancelSession(sessionId: string, runId?: string): Promise<{ command_id: string | null; run_id: string | null; status: string; already_terminal: boolean }> {
    const suffix = runId ? `?run_id=${encodeURIComponent(runId)}` : ""
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/cancel${suffix}`, { method: "POST" })
  }

  async cancelRun(runId: string): Promise<{ command_id: string | null; run_id: string | null; status: string; already_terminal: boolean }> {
    return this.request(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" })
  }

  /** 列出会话附件。 */
  async listAttachments(sessionId: string): Promise<Attachment[]> {
    return this.request<Attachment[]>(`/api/sessions/${encodeURIComponent(sessionId)}/attachments`)
  }

  /** Upload session attachments immediately; processing continues in the background. */
  async uploadSessionAttachments(sessionId: string, files: File[]): Promise<Attachment[]> {
    const form = new FormData()
    files.forEach((file) => form.append("files", file))
    return this.request<Attachment[]>(`/api/sessions/${encodeURIComponent(sessionId)}/attachments`, {
      method: "POST",
      body: form,
    })
  }

  /** 获取当前后端支持的附件类型。 */
  async getSupportedAttachmentTypes(sessionId: string): Promise<SupportedAttachmentTypes> {
    return this.request<SupportedAttachmentTypes>(`/api/sessions/${encodeURIComponent(sessionId)}/attachment-types`)
  }

  /** 软删除会话附件。 */
  async deleteAttachment(sessionId: string, fileId: string): Promise<void> {
    await this.request(`/api/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(fileId)}`, { method: "DELETE" })
  }

  // ─── 独立知识库 ───────────────────────────────────────────────

  async listKnowledgeBases(): Promise<KnowledgeBase[]> {
    return this.request<KnowledgeBase[]>("/api/knowledge-bases")
  }

  async createKnowledgeBase(name: string, description = ""): Promise<KnowledgeBase> {
    return this.request<KnowledgeBase>("/api/knowledge-bases", {
      method: "POST",
      body: JSON.stringify({ name, description }),
    })
  }

  async updateKnowledgeBase(id: string, name: string, description = ""): Promise<KnowledgeBase> {
    return this.request<KnowledgeBase>(`/api/knowledge-bases/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ name, description }),
    })
  }

  async deleteKnowledgeBase(id: string): Promise<void> {
    await this.request(`/api/knowledge-bases/${encodeURIComponent(id)}`, { method: "DELETE" })
  }

  async listKnowledgeDocuments(id: string): Promise<Attachment[]> {
    return this.request<Attachment[]>(`/api/knowledge-bases/${encodeURIComponent(id)}/documents`)
  }

  async uploadKnowledgeDocuments(id: string, files: File[]): Promise<Attachment[]> {
    const form = new FormData()
    files.forEach((file) => form.append("files", file))
    return this.request<Attachment[]>(`/api/knowledge-bases/${encodeURIComponent(id)}/documents`, {
      method: "POST",
      body: form,
    })
  }

  async deleteKnowledgeDocument(knowledgeBaseId: string, fileId: string): Promise<void> {
    await this.request(`/api/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents/${encodeURIComponent(fileId)}`, { method: "DELETE" })
  }

  async getKnowledgeDocumentVersions(knowledgeBaseId: string, attachmentId: string): Promise<Attachment[]> {
    return this.request<Attachment[]>(`/api/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents/${encodeURIComponent(attachmentId)}/versions`)
  }

  async getKnowledgeAttachmentTypes(): Promise<SupportedAttachmentTypes> {
    return this.request<SupportedAttachmentTypes>("/api/knowledge-bases/attachment-types")
  }

  // ─── 审批管理 ─────────────────────────────────────────────────

  /** 查询待处理审批，可按会话过滤。 */
  async listPendingApprovals(sessionId?: string): Promise<ApprovalRequest[]> {
    const params = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ""
    return this.request<ApprovalRequest[]>(`/api/approvals${params}`)
  }

  async respondApproval(
    approval: Pick<ApprovalRequest, "approval_id">,
    decision: "approved" | "denied",
  ): Promise<{ status: string; approval_id: string; decision: string }> {
    return this.request<{ status: string; approval_id: string; decision: string }>(
      `/api/approvals/${encodeURIComponent(approval.approval_id)}/decision`,
      {
        method: "POST",
        body: JSON.stringify({ decision }),
      },
    )
  }

  /** 展示分组中的审批仍逐条授权和提交。 */
  async respondApprovalsIndividually(
    requests: ApprovalRequest[],
    decisions: Record<string, "approved" | "denied" | "cancelled">,
  ): Promise<void> {
    const results = await Promise.allSettled(
      requests.map((request) => {
        const decision = decisions[request.approval_id]
        if (decision !== "approved" && decision !== "denied") {
          return Promise.reject(new Error("approval_decision_invalid"))
        }
        return this.respondApproval(request, decision)
      }),
    )
    const failure = results.find((result) => result.status === "rejected")
    if (failure?.status === "rejected") throw failure.reason
  }

  /** 修改会话标题。 */
  async updateSessionTitle(
    sessionId: string,
    title: string,
  ): Promise<Session> {
    return this.request<Session>(`/api/sessions/${sessionId}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    })
  }

  // ─── 工具管理 ─────────────────────────────────────────────────

  /** 查询全部工具及治理状态。 */
  async listTools(): Promise<ToolListResponse> {
    return this.request<ToolListResponse>("/api/tools")
  }

  /** 启用或停用工具。 */
  async setToolEnabled(
    name: string,
    enabled: boolean,
  ): Promise<{ status: string; name: string; enabled: boolean }> {
    return this.request<{ status: string; name: string; enabled: boolean }>(
      `/api/tools/${encodeURIComponent(name)}`,
      { method: "PATCH", body: JSON.stringify({ enabled }) },
    )
  }

  /** 更新工具的启用状态、风险等级或审批要求。 */
  async updateTool(
    name: string,
    config: {
      enabled?: boolean
      risk_level?: string
      require_approval?: boolean
    },
  ): Promise<{
    status: string
    name: string
    enabled: boolean
    risk_level: string
    require_approval: boolean
  }> {
    return this.request(
      `/api/tools/${encodeURIComponent(name)}`,
      { method: "PATCH", body: JSON.stringify(config) },
    )
  }

  // ─── 记忆管理 ─────────────────────────────────────────────────

  /** 分页查询记忆，并应用过期和会话过滤条件。 */
  async listMemories(opts?: {
    limit?: number
    offset?: number
    expired?: boolean
    session_id?: string
  }): Promise<MemoryListResponse> {
    const params = new URLSearchParams()
    if (opts?.limit != null) params.set("limit", String(opts.limit))
    if (opts?.offset != null) params.set("offset", String(opts.offset))
    if (opts?.expired) params.set("expired", "true")
    if (opts?.session_id) params.set("session_id", opts.session_id)
    const qs = params.toString()
    return this.request<MemoryListResponse>(
      `/api/memory${qs ? `?${qs}` : ""}`,
    )
  }

  /** 按 ID 获取记忆详情。 */
  async getMemory(memoryId: string): Promise<MemoryEntry> {
    return this.request<MemoryEntry>(`/api/memory/${encodeURIComponent(memoryId)}`)
  }

  async getMemoryRevisions(memoryId: string): Promise<MemoryEntry[]> {
    return this.request<MemoryEntry[]>(`/api/memory/${encodeURIComponent(memoryId)}/revisions`)
  }

  async searchMemory(query: string, nResults = 5): Promise<MemoryEntry[]> {
    return this.request<MemoryEntry[]>("/api/memory/search", {
      method: "POST",
      body: JSON.stringify({ query, n_results: nResults }),
    })
  }

  // ─── 审批日志 ─────────────────────────────────────────────────

  /** 分页查询审批日志，可按会话过滤。 */
  async listApprovalLogs(opts?: {
    session_id?: string
    limit?: number
    offset?: number
  }): Promise<ApprovalLog[]> {
    const params = new URLSearchParams()
    if (opts?.session_id) params.set("session_id", opts.session_id)
    if (opts?.limit != null) params.set("limit", String(opts.limit))
    if (opts?.offset != null) params.set("offset", String(opts.offset))
    const qs = params.toString()
    return this.request<ApprovalLog[]>(
      `/api/approvals/logs${qs ? `?${qs}` : ""}`,
    )
  }

  /** 获取审批统计数据。 */
  async getApprovalStats(): Promise<ApprovalStats> {
    return this.request<ApprovalStats>("/api/approvals/stats")
  }

  // ─── LLM 提供商 ───────────────────────────────────────────────

  /** 查询脱敏后的 LLM 提供商配置。 */
  async listProviders(): Promise<ProviderInfo[]> {
    return this.request<ProviderInfo[]>("/api/providers")
  }

  /** 使用一次性配置测试 LLM 提供商连通性。 */
  async testProvider(body: {
    provider: string
    model: string
    api_key?: string
    base_url?: string
  }): Promise<TestProviderResult> {
    return this.request<TestProviderResult>("/api/providers/test", {
      method: "POST",
      body: JSON.stringify(body),
    })
  }

  // ─── 系统设置 ─────────────────────────────────────────────────

  /** 获取只读系统设置视图；敏感密钥由后端脱敏。 */
  async getSettings(): Promise<SettingsView> {
    return this.request<SettingsView>("/api/settings")
  }

  // ─── MCP 服务器 ───────────────────────────────────────────────

  /** 查询已注册的 MCP 服务器及其连接状态。 */
  async listMcpServers(): Promise<McpServerListResponse> {
    return this.request<McpServerListResponse>("/api/mcp/servers")
  }

  /** 注册或更新 MCP 服务器配置。 */
  async registerMcpServers(payload: McpRegisterPayload): Promise<McpRegisterResponse> {
    return this.request<McpRegisterResponse>("/api/mcp/servers", {
      method: "POST",
      body: JSON.stringify(payload),
    })
  }

  /** 删除指定 MCP 服务器。 */
  async deleteMcpServer(name: string): Promise<{ status: string; name: string }> {
    return this.request<{ status: string; name: string }>(
      `/api/mcp/servers/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    )
  }

}

export const apiClient = new ApiClient()
