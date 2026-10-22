type ToolArguments = Record<string, unknown>

const TOOL_ACTIONS: Record<string, { object: string; verb: "create" | "update" | "delete" | "read" | "search" | "execute" }> = {
  "project.create_issue": { verb: "create", object: "项目事项" },
  create_issue: { verb: "create", object: "项目事项" },
  create_task: { verb: "create", object: "任务" },
  create_file: { verb: "create", object: "文件" },
  update_issue: { verb: "update", object: "项目事项" },
  delete_issue: { verb: "delete", object: "项目事项" },
  search_files: { verb: "search", object: "文件" },
  read_file: { verb: "read", object: "文件" },
}

const VERBS: Record<string, string> = {
  create: "创建",
  update: "更新",
  delete: "删除",
  read: "读取",
  search: "查找",
  list: "查看",
  get: "读取",
  write: "写入",
  upload: "上传",
  download: "下载",
  execute: "执行",
  run: "运行",
}

function actionFor(toolName: string): { verb: string; object: string } | null {
  const known = TOOL_ACTIONS[toolName.toLowerCase()]
  if (known) return { verb: VERBS[known.verb], object: known.object }
  return null
}

function argumentText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null
}

export function toolActionLabel(toolName: string, phase: "started" | "completed" | "failed"): string {
  const action = actionFor(toolName)
  if (!action) {
    if (phase === "started") return "正在准备执行一项操作"
    return phase === "failed" ? "操作未能完成" : "操作已完成"
  }
  const phrase = `${action.verb}${action.object}`
  if (phase === "started") return `正在准备${phrase}`
  return phase === "failed" ? `${phrase}未完成` : `已${phrase}`
}

export function toolStatusLabel(status?: string): string {
  const labels: Record<string, string> = {
    success: "已完成",
    failed: "未能完成",
    denied: "未获允许",
    timeout: "执行超时",
    running: "正在执行",
    pending: "等待执行",
    paused: "已暂停",
  }
  return status ? labels[status] ?? "已发起" : "已发起"
}

export function toolStatusPhase(status?: string): "started" | "completed" | "failed" {
  if (status === "running" || status === "pending" || status === "paused") return "started"
  if (status === "failed" || status === "denied" || status === "timeout") return "failed"
  return "completed"
}

export function approvalActionDescription(toolName: string, args: ToolArguments): string {
  if (toolName === "project.create_issue" || toolName === "create_issue") {
    const title = argumentText(args.title)
    const priority = argumentText(args.priority)
    const priorityLabel = priority === "high" ? "高优先级" : priority === "medium" ? "中优先级" : priority === "low" ? "低优先级" : null
    const subject = title ? `“${title}”` : "一条新事项"
    return `批准后将在项目看板创建${priorityLabel ? `${priorityLabel}的` : ""}${subject}。`
  }

  const action = actionFor(toolName)
  return action
    ? `批准后，Athena 将继续${action.verb}${action.object}。`
    : "批准后，Athena 将继续执行这项操作。"
}

export function localizedStatus(status: string): string {
  const labels: Record<string, string> = {
    running: "进行中",
    completed: "已完成",
    failed: "失败",
    waiting: "等待确认",
    paused: "已暂停",
    cancelled: "已取消",
    info: "提示",
  }
  return labels[status] || "处理中"
}
