/**
 * REST API 客户端 — 封装与后端 FastAPI 的 HTTP 通信.
 */
import type {
  CreateSessionResponse,
  Session,
  Message,
  Step,
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
  AttachmentUploadItem,
  FileTask,
  SupportedAttachmentTypes,
  EvaluationCaseSummary,
  EvaluationPage,
  EvaluationReport,
  EvaluationReportSummary,
  EvaluationSettings,
  FeedbackRecord,
  FeedbackRating,
  RetrievalEventDetail,
  RetrievalEventSummary,
} from "../types"

class ApiClient {
  private baseUrl: string = "http://127.0.0.1:8000"

  setBaseUrl(url: string): void {
    this.baseUrl = url.replace(/\/$/, "")
  }

  getBaseUrl(): string {
    return this.baseUrl
  }

  private async request<T>(
    path: string,
    options: RequestInit = {},
  ): Promise<T> {
    const url = `${this.baseUrl}${path}`
    const res = await fetch(url, {
      headers: {
        "Content-Type": "application/json",
        ...options.headers,
      },
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

  async healthCheck(): Promise<{ status: string; version: string }> {
    return this.request<{ status: string; version: string }>("/api/health")
  }

  // ─── 会话管理 ─────────────────────────────────────────────────

  async listSessions(): Promise<Session[]> {
    return this.request<Session[]>("/api/sessions")
  }

  async createSession(title: string = "New Session"): Promise<CreateSessionResponse> {
    return this.request<CreateSessionResponse>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ title }),
    })
  }

  async getSession(sessionId: string): Promise<Session> {
    return this.request<Session>(`/api/sessions/${sessionId}`)
  }

  async deleteSession(sessionId: string): Promise<{ status: string }> {
    return this.request<{ status: string }>(`/api/sessions/${sessionId}`, {
      method: "DELETE",
    })
  }

  async getMessages(sessionId: string, limit?: number): Promise<Message[]> {
    const params = limit ? `?limit=${limit}` : ""
    return this.request<Message[]>(`/api/sessions/${sessionId}/messages${params}`)
  }

  async getSteps(sessionId: string): Promise<Step[]> {
    return this.request<Step[]>(`/api/sessions/${sessionId}/steps`)
  }

  async getToolCalls(sessionId: string, status?: string): Promise<unknown[]> {
    const params = status ? `?status=${status}` : ""
    return this.request<unknown[]>(`/api/sessions/${sessionId}/tool_calls${params}`)
  }

  async stopSession(sessionId: string): Promise<{ status: string }> {
    return this.request<{ status: string }>(`/api/sessions/${sessionId}/stop`, {
      method: "POST",
    })
  }

  async uploadAttachments(sessionId: string, files: File[]): Promise<AttachmentUploadItem[]> {
    const form = new FormData()
    files.forEach((file) => form.append("files", file))
    const response = await fetch(`${this.baseUrl}/api/sessions/${sessionId}/attachments`, {
      method: "POST",
      body: form,
    })
    if (!response.ok) throw new Error(`Upload failed: ${response.status} ${await response.text()}`)
    const data = await response.json() as { items: AttachmentUploadItem[] }
    return data.items
  }

  async listAttachments(sessionId: string): Promise<Attachment[]> {
    return this.request<Attachment[]>(`/api/sessions/${sessionId}/attachments`)
  }

  async getSupportedAttachmentTypes(sessionId: string): Promise<SupportedAttachmentTypes> {
    return this.request<SupportedAttachmentTypes>(`/api/sessions/${sessionId}/attachment-types`)
  }

  async deleteAttachment(sessionId: string, fileId: string): Promise<void> {
    await this.request(`/api/sessions/${sessionId}/attachments/${fileId}`, { method: "DELETE" })
  }

  async retryAttachment(sessionId: string, fileId: string): Promise<AttachmentUploadItem> {
    return this.request<AttachmentUploadItem>(`/api/sessions/${sessionId}/attachments/${fileId}/retry`, { method: "POST" })
  }

  async getFileTask(sessionId: string, taskId: string): Promise<FileTask> {
    return this.request<FileTask>(`/api/sessions/${sessionId}/file-tasks/${taskId}`)
  }

  // ─── 审批管理 ─────────────────────────────────────────────────

  async listPendingApprovals(sessionId?: string): Promise<ApprovalRequest[]> {
    const params = sessionId ? `?session_id=${sessionId}` : ""
    return this.request<ApprovalRequest[]>(`/api/approvals${params}`)
  }

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

  async cancelApproval(approvalId: string): Promise<{ status: string }> {
    return this.request<{ status: string }>(`/api/approvals/${approvalId}/cancel`, {
      method: "POST",
    })
  }

  async getApprovalLogs(sessionId: string): Promise<unknown[]> {
    return this.request<unknown[]>(`/api/approvals/logs/${sessionId}`)
  }

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

  async listTools(): Promise<ToolListResponse> {
    return this.request<ToolListResponse>("/api/tools")
  }

  async setToolEnabled(
    name: string,
    enabled: boolean,
  ): Promise<{ status: string; name: string; enabled: boolean }> {
    return this.request<{ status: string; name: string; enabled: boolean }>(
      `/api/tools/${encodeURIComponent(name)}`,
      { method: "PATCH", body: JSON.stringify({ enabled }) },
    )
  }

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

  async getMemory(memoryId: string): Promise<MemoryEntry> {
    return this.request<MemoryEntry>(`/api/memory/${memoryId}`)
  }

  async deleteMemory(memoryId: string): Promise<{ status: string }> {
    return this.request<{ status: string }>(`/api/memory/${memoryId}`, {
      method: "DELETE",
    })
  }

  async setMemoryPinned(
    memoryId: string,
    pinned: boolean,
  ): Promise<{ status: string }> {
    return this.request<{ status: string }>(
      `/api/memory/${memoryId}/pin?pinned=${pinned}`,
      { method: "POST" },
    )
  }

  async updateMemory(
    memoryId: string,
    content: string,
  ): Promise<{ status: string; memory_id: string }> {
    return this.request<{ status: string; memory_id: string }>(
      `/api/memory/${memoryId}`,
      { method: "PATCH", body: JSON.stringify({ content }) },
    )
  }

  // ─── 审批日志 ─────────────────────────────────────────────────

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

  async getApprovalStats(): Promise<ApprovalStats> {
    return this.request<ApprovalStats>("/api/approvals/stats")
  }

  // ─── LLM 提供商 ───────────────────────────────────────────────

  async listProviders(): Promise<ProviderInfo[]> {
    return this.request<ProviderInfo[]>("/api/providers")
  }

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

  async getSettings(): Promise<SettingsView> {
    return this.request<SettingsView>("/api/settings")
  }

  // ─── MCP 服务器 ───────────────────────────────────────────────

  async listMcpServers(): Promise<McpServerListResponse> {
    return this.request<McpServerListResponse>("/api/mcp/servers")
  }

  async registerMcpServers(payload: McpRegisterPayload): Promise<McpRegisterResponse> {
    return this.request<McpRegisterResponse>("/api/mcp/servers", {
      method: "POST",
      body: JSON.stringify(payload),
    })
  }

  async deleteMcpServer(name: string): Promise<{ status: string; name: string }> {
    return this.request<{ status: string; name: string }>(
      `/api/mcp/servers/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    )
  }

  // ─── 检索评估 ────────────────────────────────────────────────

  async listEvaluationEvents(opts?: {
    session_id?: string
    source?: string
    feedback_status?: string
    limit?: number
    cursor?: string
  }): Promise<EvaluationPage<RetrievalEventSummary>> {
    const params = new URLSearchParams()
    Object.entries(opts ?? {}).forEach(([key, value]) => {
      if (value != null && value !== "") params.set(key, String(value))
    })
    return this.request<EvaluationPage<RetrievalEventSummary>>(
      `/api/evaluation/events${params.size ? `?${params.toString()}` : ""}`,
    )
  }

  async getEvaluationEvent(eventId: string): Promise<RetrievalEventDetail> {
    return this.request<RetrievalEventDetail>(`/api/evaluation/events/${encodeURIComponent(eventId)}`)
  }

  async submitFeedback(payload: {
    event_id: string
    rating: FeedbackRating
    correct_result_ids?: string[]
    expect_empty?: boolean
    gain?: 1 | 2 | 3
    comment?: string
  }): Promise<FeedbackRecord> {
    return this.request<FeedbackRecord>("/api/evaluation/feedback", {
      method: "POST",
      body: JSON.stringify(payload),
    })
  }

  async listEvaluationFeedback(opts?: {
    status?: string
    rating?: FeedbackRating
    session_id?: string
    limit?: number
    cursor?: string
  }): Promise<EvaluationPage<FeedbackRecord>> {
    const params = new URLSearchParams()
    Object.entries(opts ?? {}).forEach(([key, value]) => {
      if (value != null && value !== "") params.set(key, String(value))
    })
    return this.request<EvaluationPage<FeedbackRecord>>(
      `/api/evaluation/feedback${params.size ? `?${params.toString()}` : ""}`,
    )
  }

  async promoteEvaluationFeedback(
    feedbackId: string,
    payload?: { case_id?: string; labels?: string[] },
  ): Promise<{ case: EvaluationCaseSummary; dataset_version: string }> {
    return this.request<{ case: EvaluationCaseSummary; dataset_version: string }>(
      `/api/evaluation/feedback/${encodeURIComponent(feedbackId)}/promote`,
      { method: "POST", body: JSON.stringify(payload ?? {}) },
    )
  }

  async listEvaluationCases(opts?: {
    label?: string
    source?: string
    status?: string
    limit?: number
    cursor?: string
  }): Promise<EvaluationPage<EvaluationCaseSummary>> {
    const params = new URLSearchParams()
    Object.entries(opts ?? {}).forEach(([key, value]) => {
      if (value != null && value !== "") params.set(key, String(value))
    })
    return this.request<EvaluationPage<EvaluationCaseSummary>>(
      `/api/evaluation/cases${params.size ? `?${params.toString()}` : ""}`,
    )
  }

  async listEvaluationReports(opts?: {
    kind?: "run" | "comparison"
    limit?: number
    cursor?: string
  }): Promise<EvaluationPage<EvaluationReportSummary>> {
    const params = new URLSearchParams()
    Object.entries(opts ?? {}).forEach(([key, value]) => {
      if (value != null && value !== "") params.set(key, String(value))
    })
    return this.request<EvaluationPage<EvaluationReportSummary>>(
      `/api/evaluation/reports${params.size ? `?${params.toString()}` : ""}`,
    )
  }

  async getEvaluationReport(reportId: string): Promise<EvaluationReport> {
    return this.request<EvaluationReport>(`/api/evaluation/reports/${encodeURIComponent(reportId)}`)
  }

  async getEvaluationSettings(): Promise<EvaluationSettings> {
    return this.request<EvaluationSettings>("/api/evaluation/settings")
  }

  async updateEvaluationSettings(payload: {
    record_enabled?: boolean
    record_sample_rate?: number
  }): Promise<EvaluationSettings> {
    return this.request<EvaluationSettings>("/api/evaluation/settings", {
      method: "PATCH",
      body: JSON.stringify(payload),
    })
  }

  async clearEvaluationRecords(targets: Array<"records" | "feedback" | "cases" | "reports">): Promise<void> {
    await this.request("/api/evaluation/records", {
      method: "DELETE",
      body: JSON.stringify({ targets }),
    })
  }
}

export const apiClient = new ApiClient()
