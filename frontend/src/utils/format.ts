/**
 * 将 ISO 时间字符串格式化为中文本地时间。
 * @param iso ISO 8601 时间；为空或无法解析时原样返回占位值/输入值。
 * @returns string 格式化后的日期时间。
 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—"
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  })
}

/**
 * 将 ISO 时间转换为相对时间文本。
 * @param iso ISO 8601 时间；为空或无法解析时返回占位值/输入值。
 * @returns string “刚刚”或距离当前时间的分钟、小时、天数。
 */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—"
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const diffMs = Date.now() - d.getTime()
  const minutes = Math.floor(diffMs / 60000)
  if (minutes < 1) return "刚刚"
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  return `${days} 天前`
}

/**
 * 截断超过长度限制的文本。
 * @param text 原始文本。
 * @param max 最大字符数，必须为非负数。
 * @returns string 截断后的文本，截断时追加省略号。
 */
export function truncate(text: string, max = 120): string {
  if (text.length <= max) return text
  return `${text.slice(0, max)}…`
}
