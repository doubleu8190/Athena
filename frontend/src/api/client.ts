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
} from "../types"

class ApiClient {
  private baseUrl = ""

  /** 设置 API 根地址；会移除末尾斜杠以避免拼接出双斜杠。 */
  setBaseUrl(url: string): void {
    this.baseUrl = url.replace(/\/$/, "")
  }

  /** 返回当前 API 根地址。 */
  getBaseUrl(): string {
    return this.baseUrl
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
    })
    if (!res.ok) {
      const errorText = await res.text().catch(() => res.statusText)
      throw new Error(`API ${res.status}: ${errorText}`)
    }
    if (res.status === 204) return undefined as T
    return res.json()
  }

  // ─── 健康检查 ─────────────────────────────────────────────────

  /** 获取后端健康状态和版本信息。 */
  async healthCheck(): Promise<{ status: string; version: string }> {
    return this.request<{ status: string; version: string }>("/api/health")
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

  /** 提交异步 Agent 运行命令。
   * @param sessionId 目标会话 ID，必须非空。
   * @param payload 用户消息和可选附件/幂等命令 ID。
   * @returns Promise 包含命令 ID、运行 ID及排队状态。
   * @throws Error 当会话不存在、命令冲突或会话不可并行运行时抛出。
   */
  async submitRun(sessionId: string, payload: { message: string; files?: File[]; command_id?: string }): Promise<{ command_id: string; run_id?: string; message_id: string; status: string; deduplicated?: boolean }> {
    const commandId = payload.command_id || `cmd_${crypto.randomUUID()}`
    if (payload.files && payload.files.length > 0) {
      const form = new FormData()
      form.append("message", payload.message)
      form.append("command_id", commandId)
      payload.files.forEach((file) => form.append("files", file))
      return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/runs`, {
        method: "POST", body: form,
      })
    }
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/runs`, {
      method: "POST", body: JSON.stringify({ message: payload.message, command_id: commandId }),
    })
  }

  /** 提交暂停会话命令。 */
  async pauseSession(sessionId: string): Promise<{ command_id: string; status: string }> {
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/pause`, { method: "POST" })
  }

  /** 提交恢复会话命令。 */
  async resumeSession(sessionId: string): Promise<{ command_id: string; status: string }> {
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/resume`, { method: "POST" })
  }

  /** 提交取消运行命令。
   * @param runId 可选目标运行 ID；为空时由后端选择当前运行。
   */
  async cancelSession(sessionId: string, runId?: string): Promise<{ command_id: string; status: string }> {
    const suffix = runId ? `?run_id=${encodeURIComponent(runId)}` : ""
    return this.request(`/api/sessions/${encodeURIComponent(sessionId)}/cancel${suffix}`, { method: "POST" })
  }

  /** 列出会话附件。 */
  async listAttachments(sessionId: string): Promise<Attachment[]> {
    return this.request<Attachment[]>(`/api/sessions/${sessionId}/attachments`)
  }

  /** 获取当前后端支持的附件类型。 */
  async getSupportedAttachmentTypes(sessionId: string): Promise<SupportedAttachmentTypes> {
    return this.request<SupportedAttachmentTypes>(`/api/sessions/${sessionId}/attachment-types`)
  }

  /** 软删除会话附件。 */
  async deleteAttachment(sessionId: string, fileId: string): Promise<void> {
    await this.request(`/api/sessions/${sessionId}/attachments/${fileId}`, { method: "DELETE" })
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

  async getKnowledgeAttachmentTypes(): Promise<SupportedAttachmentTypes> {
    return this.request<SupportedAttachmentTypes>("/api/knowledge-bases/attachment-types")
  }

  // ─── 审批管理 ─────────────────────────────────────────────────

  /** 查询待处理审批，可按会话过滤。 */
  async listPendingApprovals(sessionId?: string): Promise<ApprovalRequest[]> {
    const params = sessionId ? `?session_id=${sessionId}` : ""
    return this.request<ApprovalRequest[]>(`/api/approvals${params}`)
  }

  /** 提交审批允许或拒绝动作。 */
  async respondApproval(
    approvalId: string,
    action: "allow" | "deny",
  ): Promise<{ status: string }> {
    return this.request<{ status: string }>(
      `/api/approvals/${approvalId}/respond`,
      {
        method: "POST",
        body: JSON.stringify({ action }),
      },
    )
  }

  /** 提交取消审批命令。 */
  async cancelApproval(approvalId: string): Promise<{ status: string }> {
    return this.request<{ status: string }>(`/api/approvals/${approvalId}/cancel`, {
      method: "POST",
    })
  }

  /** 查询指定会话的审批日志。 */
  async getApprovalLogs(sessionId: string): Promise<unknown[]> {
    return this.request<unknown[]>(`/api/approvals/logs/${sessionId}`)
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

  /** 分页查询记忆，并应用固定、过期和会话过滤条件。 */
  async listMemories(opts?: {
    limit?: number
    offset?: number
    pinned?: boolean
    expired?: boolean
    session_id?: string
  }): Promise<MemoryListResponse> {
    const params = new URLSearchParams()
    if (opts?.limit != null) params.set("limit", String(opts.limit))
    if (opts?.offset != null) params.set("offset", String(opts.offset))
    if (opts?.pinned) params.set("pinned", "true")
    if (opts?.expired) params.set("expired", "true")
    if (opts?.session_id) params.set("session_id", opts.session_id)
    const qs = params.toString()
    return this.request<MemoryListResponse>(
      `/api/memory${qs ? `?${qs}` : ""}`,
    )
  }

  /** 按 ID 获取记忆详情。 */
  async getMemory(memoryId: string): Promise<MemoryEntry> {
    return this.request<MemoryEntry>(`/api/memory/${memoryId}`)
  }

  /** 删除记忆条目。 */
  async deleteMemory(memoryId: string): Promise<{ status: string }> {
    return this.request<{ status: string }>(`/api/memory/${memoryId}`, {
      method: "DELETE",
    })
  }

  /** 设置记忆的固定状态。 */
  async setMemoryPinned(
    memoryId: string,
    pinned: boolean,
  ): Promise<{ status: string }> {
    return this.request<{ status: string }>(
      `/api/memory/${memoryId}/pin?pinned=${pinned}`,
      { method: "POST" },
    )
  }

  /** 更新记忆正文。 */
  async updateMemory(
    memoryId: string,
    content: string,
  ): Promise<{ status: string; memory_id: string; revised_from: string }> {
    return this.request<{ status: string; memory_id: string; revised_from: string }>(
      `/api/memory/${memoryId}`,
      { method: "PATCH", body: JSON.stringify({ content }) },
    )
  }

  /** 更新记忆的事实有效性，不影响访问热度和保留期限。 */
  async setMemoryValidity(
    memoryId: string,
    validityStatus: "valid" | "uncertain" | "invalid",
    validUntil?: string | null,
  ): Promise<{ status: string; memory_id: string; validity_status: string; valid_until: string | null }> {
    return this.request(
      `/api/memory/${memoryId}/validity`,
      {
        method: "PATCH",
        body: JSON.stringify({ validity_status: validityStatus, valid_until: validUntil ?? null }),
      },
    )
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
