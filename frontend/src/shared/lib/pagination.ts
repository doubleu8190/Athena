/**
 * 将 URL 查询参数中的页码解析为从 1 开始的正整数；
 * 缺失、非整数或小于 1 时统一回退为第 1 页。
 */
export function parsePage(value: string | null): number {
  if (!value) {
    return 1;
  }
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 1;
}
