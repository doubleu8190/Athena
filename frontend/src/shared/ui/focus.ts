export const FOCUSABLE_SELECTOR = [
  'button:not(:disabled):not([disabled]):not([tabindex="-1"])',
  'a[href]:not([disabled]):not([tabindex="-1"])',
  'input:not(:disabled):not([disabled]):not([tabindex="-1"])',
  'select:not(:disabled):not([disabled]):not([tabindex="-1"])',
  'textarea:not(:disabled):not([disabled]):not([tabindex="-1"])',
  '[tabindex]:not([tabindex="-1"]):not(:disabled):not([disabled])',
].join(",");

/**
 * 统一弹层内的可聚焦控件发现逻辑，避免不同浮层各自维护一套焦点循环。
 * 这里不按实际尺寸过滤，兼容 jsdom 测试环境与隐藏控件的浏览器行为。
 */
export function getFocusableElements(
  container: HTMLElement,
): HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
  );
}
