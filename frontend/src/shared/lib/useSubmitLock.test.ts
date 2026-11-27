import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useSubmitLock } from "./useSubmitLock";

function createDeferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, reject, resolve };
}

describe("useSubmitLock", () => {
  it("同 tick 并发调用仅放行首次任务，并在完成后释放锁且保持 runLocked 引用稳定", async () => {
    const { result, rerender } = renderHook(() => useSubmitLock());
    const initialRunLocked = result.current.runLocked;
    expect(result.current.isLocked).toBe(false);
    expect(result.current.submitting).toBe(false);

    const deferred = createDeferred<string>();
    const firstTask = vi.fn(() => deferred.promise);
    const secondTask = vi.fn(async () => "second");

    let firstPromise!: Promise<string | undefined>;
    let secondPromise!: Promise<string | undefined>;

    await act(async () => {
      firstPromise = result.current.runLocked(firstTask);
      secondPromise = result.current.runLocked(secondTask);
      await Promise.resolve();
    });

    expect(firstTask).toHaveBeenCalledTimes(1);
    expect(secondTask).not.toHaveBeenCalled();
    expect(await secondPromise).toBeUndefined();
    expect(result.current.isLocked).toBe(true);
    expect(result.current.submitting).toBe(true);
    expect(result.current.runLocked).toBe(initialRunLocked);

    await act(async () => {
      deferred.resolve("ok");
      await firstPromise;
    });

    expect(await firstPromise).toBe("ok");
    expect(result.current.isLocked).toBe(false);
    expect(result.current.submitting).toBe(false);

    rerender();
    expect(result.current.runLocked).toBe(initialRunLocked);

    let thirdResult: string | undefined;
    await act(async () => {
      thirdResult = await result.current.runLocked(secondTask);
    });
    expect(thirdResult).toBe("second");
    expect(secondTask).toHaveBeenCalledTimes(1);
  });

  it("任务抛出异常时在 finally 释放锁，允许后续重新提交", async () => {
    const { result } = renderHook(() => useSubmitLock());
    const failingTask = vi.fn(async () => {
      throw new Error("boom");
    });

    await act(async () => {
      await expect(result.current.runLocked(failingTask)).rejects.toThrow(
        "boom",
      );
    });

    expect(result.current.isLocked).toBe(false);
    expect(result.current.submitting).toBe(false);

    const recoveryTask = vi.fn(async () => "recovered");
    let recovered: string | undefined;
    await act(async () => {
      recovered = await result.current.runLocked(recoveryTask);
    });
    expect(recovered).toBe("recovered");
    expect(recoveryTask).toHaveBeenCalledTimes(1);
  });

  it("组件在任务完成前卸载时不触发卸载后的 setState", async () => {
    const consoleErrorSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    try {
      const { result, unmount } = renderHook(() => useSubmitLock());
      const deferred = createDeferred<string>();

      let pendingPromise!: Promise<string | undefined>;
      await act(async () => {
        pendingPromise = result.current.runLocked(() => deferred.promise);
        await Promise.resolve();
      });

      expect(result.current.isLocked).toBe(true);
      unmount();

      await act(async () => {
        deferred.resolve("done-after-unmount");
        await pendingPromise;
      });

      expect(await pendingPromise).toBe("done-after-unmount");
      expect(consoleErrorSpy).not.toHaveBeenCalled();
    } finally {
      consoleErrorSpy.mockRestore();
    }
  });
});
