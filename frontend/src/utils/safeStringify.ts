/**
 * 防御性序列化:
 * - 循环引用 → 替换为 "[Circular]" 而非抛错(JSON.stringify 会直接抛 TypeError)
 * - 超大文本/对象 → 截断并在尾部标注被截掉的字符数,避免巨型 DOM 拖垮渲染
 * 借鉴 OpenClaw PR #9248 对工具参数的 sanitizeForDisplay 思路。
 */

/**
 * 按字符数截断字符串。
 * @param s 原始字符串。
 * @param maxLen 最大字符数，必须为非负数。
 * @returns string 截断后的字符串。
 */
export function truncateString(s: string, maxLen: number): string {
  if (s.length <= maxLen) return s
  return `${s.slice(0, maxLen)}… (truncated ${s.length - maxLen} chars)`
}

/**
 * 安全地将任意值转换为适合界面展示的字符串。
 * @param value 待转换值，可包含循环引用。
 * @param maxLen 输出最大字符数，默认 8000。
 * @returns string JSON 文本或降级后的字符串，并按长度截断。
 * @throws 不向调用方抛出序列化异常；无法序列化时使用字符串降级。
 */
export function safeStringify(value: unknown, maxLen = 8000): string {
  if (typeof value === "string") {
    return truncateString(value, maxLen)
  }
  if (value === null || value === undefined) {
    return String(value)
  }
  let json: string
  try {
    const seen = new WeakSet<object>()
    json = JSON.stringify(
      value,
      (_key, val) => {
        if (val !== null && typeof val === "object") {
          if (seen.has(val)) return "[Circular]"
          seen.add(val)
        }
        return val
      },
      2,
    )
  } catch {
    return truncateString(String(value), maxLen)
  }
  return truncateString(json ?? String(value), maxLen)
}
