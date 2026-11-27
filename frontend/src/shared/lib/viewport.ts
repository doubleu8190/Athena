import { useEffect, useState } from "react";

export const DEFAULT_DESKTOP_BREAKPOINT_PX = 1024;

export interface UseIsDesktopViewportOptions {
  /** 桌面端最小宽度断点（单位 px），默认 1024。 */
  breakpointPx?: number;
  /**
   * 视口检测策略：
   * - "matchMedia"：优先使用 window.matchMedia(`(min-width: ${breakpointPx}px)`)，缺失时回退 innerWidth；
   * - "innerWidth"：直接以 window.innerWidth >= breakpointPx 判定，同时监听 resize 与 matchMedia 变化。
   */
  strategy?: "innerWidth" | "matchMedia";
}

function readIsDesktopViewport(
  breakpointPx: number,
  strategy: "innerWidth" | "matchMedia",
): boolean {
  if (typeof window === "undefined") {
    return true;
  }
  if (strategy === "matchMedia" && typeof window.matchMedia === "function") {
    return window.matchMedia(`(min-width: ${breakpointPx}px)`).matches;
  }
  return window.innerWidth >= breakpointPx;
}

/**
 * 监听桌面端视口断点（默认 >= 1024px），在桌面端与移动端/窄屏布局间同步切换状态。
 */
export function useIsDesktopViewport(
  optionsOrBreakpoint: number | UseIsDesktopViewportOptions = DEFAULT_DESKTOP_BREAKPOINT_PX,
): boolean {
  const breakpointPx =
    typeof optionsOrBreakpoint === "number"
      ? optionsOrBreakpoint
      : (optionsOrBreakpoint.breakpointPx ?? DEFAULT_DESKTOP_BREAKPOINT_PX);
  const strategy =
    typeof optionsOrBreakpoint === "number"
      ? "matchMedia"
      : (optionsOrBreakpoint.strategy ?? "matchMedia");

  const [isDesktop, setIsDesktop] = useState<boolean>(() =>
    readIsDesktopViewport(breakpointPx, strategy),
  );

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }

    const query = `(min-width: ${breakpointPx}px)`;
    const mediaQuery =
      typeof window.matchMedia === "function" ? window.matchMedia(query) : null;

    if (strategy === "matchMedia" && mediaQuery) {
      setIsDesktop(mediaQuery.matches);
      const handleChange = (event: MediaQueryListEvent) => {
        setIsDesktop(event.matches);
      };
      if (typeof mediaQuery.addEventListener === "function") {
        mediaQuery.addEventListener("change", handleChange);
        return () => mediaQuery.removeEventListener("change", handleChange);
      }
      // 兼容旧版 Safari MediaQueryList 订阅 API，同时防御测试轻量 mock 缺省该方法。
      if (typeof mediaQuery.addListener === "function") {
        mediaQuery.addListener(handleChange);
        return () => {
          if (typeof mediaQuery.removeListener === "function") {
            mediaQuery.removeListener(handleChange);
          }
        };
      }
      return;
    }

    const updateFromWidth = () => {
      setIsDesktop(window.innerWidth >= breakpointPx);
    };
    updateFromWidth();
    window.addEventListener("resize", updateFromWidth);
    if (mediaQuery) {
      if (typeof mediaQuery.addEventListener === "function") {
        mediaQuery.addEventListener("change", updateFromWidth);
      } else if (typeof mediaQuery.addListener === "function") {
        mediaQuery.addListener(updateFromWidth);
      }
    }
    return () => {
      window.removeEventListener("resize", updateFromWidth);
      if (mediaQuery) {
        if (typeof mediaQuery.removeEventListener === "function") {
          mediaQuery.removeEventListener("change", updateFromWidth);
        } else if (typeof mediaQuery.removeListener === "function") {
          mediaQuery.removeListener(updateFromWidth);
        }
      }
    };
  }, [breakpointPx, strategy]);

  return isDesktop;
}
