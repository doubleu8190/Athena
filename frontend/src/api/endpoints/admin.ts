import { api } from '../client'

// ── Types ─────────────────────────────────────────────────────────────

export interface DashboardMetrics {
  completed: number
  running: number
  pending: number
  failed: number
  success_rate_24h: number
  harness_blocks_24h: number
}

export interface MCPServer {
  server_id: string
  name: string
  transport: string
  enabled: boolean
  connection_status: string
  source: string
}

export interface MCPServerCreate {
  server_id: string
  name: string
  transport: string
  connection_config: Record<string, unknown>
  source?: string
}

export interface Skill {
  skill_id: string
  name: string
  version: string
  image_uri: string
  allowed_domains: string | null
  status: string
}

export interface SkillInstall {
  name: string
  version: string
  image_uri: string
  allowed_domains?: string
}

export interface Device {
  device_id: string
  type: string
  status: string
  connection_info: Record<string, unknown> | null
  last_heartbeat: string | null
}

export interface DeviceRegister {
  device_id: string
  type: string
  connection_info?: Record<string, unknown>
}

export interface HarnessRule {
  rule_id: string
  priority: number
  rule_type: string
  name: string
  description: string
  enabled: boolean
  revision: number
  config: Record<string, unknown>
}

export interface HarnessRuleCreate {
  rule_id: string
  rule_type: string
  name: string
  description?: string
  config_json: Record<string, unknown>
  priority?: number
  enabled?: boolean
}

export interface HarnessRuleUpdate {
  config_json?: Record<string, unknown>
  priority?: number
  enabled?: boolean
}

export interface PathPermissionConfig {
  path: string
  permissions: "r" | "w" | "rw"
}

export interface AuditLog {
  event_id: string
  event_type: string
  actor_user_id: string | null
  details: Record<string, unknown> | null
  timestamp: string
}

export interface PaginatedResult<T> {
  items: T[]
  next_cursor: string | null
}

// ── Backend response wrappers ──────────────────────────────────────────

/** Backend wraps all responses in `{ code, message, data }` */
interface BackendResponse<T> {
  code: number
  message: string
  data: T
}

/** Backend list endpoints return `{ items: [...] }` inside `data` */
interface BackendList<T> {
  items: T[]
}

/** Backend paginated list endpoints return `{ items: [...], next_cursor }` */
interface BackendPaginatedList<T> {
  items: T[]
  next_cursor: string | null
}

// ── Dashboard ──────────────────────────────────────────────────────────

export function fetchDashboard(): Promise<DashboardMetrics> {
  return api
    .get<BackendResponse<{
      tasks: Record<string, number>
      subtask_success_rate: number
      total_subtasks_24h: number
      harness_blocks_24h: number
    }>>('/admin/dashboard')
    .then((res) => ({
      completed: res.data.tasks?.completed ?? 0,
      running: res.data.tasks?.running ?? 0,
      pending: res.data.tasks?.pending ?? 0,
      failed: res.data.tasks?.failed ?? 0,
      success_rate_24h: res.data.subtask_success_rate ?? 0,
      harness_blocks_24h: res.data.harness_blocks_24h ?? 0,
    }))
}

// ── MCP Servers ────────────────────────────────────────────────────────

export function fetchMCPServers(): Promise<MCPServer[]> {
  return api
    .get<BackendResponse<BackendList<MCPServer>>>('/admin/mcp-servers')
    .then((res) => res.data.items ?? [])
}

export function createMCPServer(body: MCPServerCreate): Promise<void> {
  return api.post('/admin/mcp-servers', body).then(() => undefined)
}

export function deleteMCPServer(id: string): Promise<void> {
  return api.delete(`/admin/mcp-servers/${id}`).then(() => undefined)
}

export function updateMCPServerStatus(id: string, enabled: boolean): Promise<void> {
  return api.put(`/admin/mcp-servers/${id}/status`, { enabled }).then(() => undefined)
}

// ── Skills ─────────────────────────────────────────────────────────────

export function fetchSkills(): Promise<Skill[]> {
  return api
    .get<BackendResponse<BackendList<Skill>>>('/admin/skills')
    .then((res) => res.data.items ?? [])
}

export function installSkill(body: SkillInstall): Promise<void> {
  return api.post('/admin/skills', body).then(() => undefined)
}

export function uninstallSkill(id: string): Promise<void> {
  return api.delete(`/admin/skills/${id}`).then(() => undefined)
}

// ── Devices ────────────────────────────────────────────────────────────

export function fetchDevices(): Promise<Device[]> {
  return api
    .get<BackendResponse<BackendList<Device>>>('/admin/devices')
    .then((res) => res.data.items ?? [])
}

export function registerDevice(body: DeviceRegister): Promise<void> {
  return api.post('/admin/devices', body).then(() => undefined)
}

export function deregisterDevice(id: string): Promise<void> {
  return api.delete(`/admin/devices/${id}`).then(() => undefined)
}

// ── Harness Rules ──────────────────────────────────────────────────────

export function fetchHarnessRules(): Promise<HarnessRule[]> {
  return api
    .get<BackendResponse<BackendList<HarnessRule>>>('/admin/harness/rules')
    .then((res) => res.data.items ?? [])
}

export function createHarnessRule(body: HarnessRuleCreate): Promise<void> {
  return api.post('/admin/harness/rules', body).then(() => undefined)
}

export function updateHarnessRule(id: string, body: HarnessRuleUpdate): Promise<void> {
  return api.put(`/admin/harness/rules/${id}`, body).then(() => undefined)
}

export function reloadHarnessCache(): Promise<void> {
  return api.post('/admin/harness/reload').then(() => undefined)
}

// ── Audit Logs ─────────────────────────────────────────────────────────

export function fetchAuditLogs(params?: {
  event_type?: string
  cursor?: string
  limit?: number
}): Promise<PaginatedResult<AuditLog>> {
  const queryParams: Record<string, string> = {}
  if (params?.event_type) queryParams.event_type = params.event_type
  if (params?.cursor) queryParams.cursor = params.cursor
  if (params?.limit) queryParams.limit = String(params.limit)
  return api
    .get<BackendResponse<BackendPaginatedList<AuditLog>>>('/admin/audit-logs', queryParams)
    .then((res) => ({
      items: res.data.items ?? [],
      next_cursor: res.data.next_cursor ?? null,
    }))
}

// ── IM Status ──────────────────────────────────────────────────────────

export function fetchIMStatus(): Promise<{ data: Record<string, unknown> }> {
  return api.get('/admin/im/status')
}

// ── Memory ─────────────────────────────────────────────────────────────

export interface Memory {
  memory_id: string
  key: string
  value: string
  meta: Record<string, unknown>
  updated_at: string
}

export interface MemorySearchResult extends Memory {
  score: number
}

export function fetchMemories(params?: {
  key_prefix?: string
  limit?: number
}): Promise<Memory[]> {
  const query: Record<string, string> = {}
  if (params?.key_prefix) query.key_prefix = params.key_prefix
  if (params?.limit) query.limit = String(params.limit)
  return api
    .get<BackendResponse<{ memories: Memory[]; count: number }>>('/memory/list', query)
    .then((res) => res.data.memories ?? [])
}

export function createMemory(body: {
  key: string
  value: string
  meta?: Record<string, unknown>
}): Promise<Memory> {
  return api
    .post<BackendResponse<Memory>>('/memory', body)
    .then((res) => res.data)
}

export function updateMemory(
  memoryId: string,
  body: { value: string; meta?: Record<string, unknown> },
): Promise<Memory> {
  return api
    .put<BackendResponse<Memory>>(`/memory/${memoryId}`, body)
    .then((res) => res.data)
}

export function deleteMemory(memoryId: string): Promise<void> {
  return api.delete(`/memory/${memoryId}`).then(() => undefined)
}

export function searchMemories(body: {
  query: string
  top_k?: number
  type?: string
}): Promise<MemorySearchResult[]> {
  return api
    .post<BackendResponse<{ memories: MemorySearchResult[]; count: number }>>(
      '/memory/search',
      body,
    )
    .then((res) => res.data.memories ?? [])
}
