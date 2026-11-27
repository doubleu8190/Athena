import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export interface SubmitLock {
  /** 是否处于提交锁定态（适合用于禁用表单按钮与弹窗关闭保护） */
  isLocked: boolean;
  /** 同步获取提交锁并执行异步任务；已锁定时直接忽略并返回 undefined */
  runLocked: <T>(task: () => Promise<T>) => Promise<T | undefined>;
  /** isLocked 的语义别名，便于表单/弹窗状态直接透传 */
  submitting: boolean;
}

/**
 * 轻量公共表单提交锁 Hook：
 * - 通过同步 ref 拦截同一 render tick 内的双击连点；
 * - 通过 mountedRef 确保组件卸载后结束的异步任务不会触发 setState；
 * - 保证无论任务成功还是抛错都在 finally 中稳定释放锁。
 */
export function useSubmitLock(): SubmitLock {
  const lockedRef = useRef(false);
  const mountedRef = useRef(true);
  const [isLocked, setIsLocked] = useState(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const runLocked = useCallback(
    async <T>(task: () => Promise<T>): Promise<T | undefined> => {
      // 同一 render tick 内立即拦截重复点击，不依赖异步 state 刷新窗口。
      if (lockedRef.current) {
        return undefined;
      }
      lockedRef.current = true;
      if (mountedRef.current) {
        setIsLocked(true);
      }
      try {
        return await task();
      } finally {
        lockedRef.current = false;
        // 卸载后跳过状态回写，避免异步收尾触发 React 卸载警告。
        if (mountedRef.current) {
          setIsLocked(false);
        }
      }
    },
    [],
  );

  return useMemo(
    () => ({
      isLocked,
      runLocked,
      submitting: isLocked,
    }),
    [isLocked, runLocked],
  );
}
