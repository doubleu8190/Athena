import { useEffect, useRef } from 'react'

/**
 * Calls `callback` immediately, then every `intervalMs` milliseconds.
 * Cleans up the interval on unmount.
 *
 * Uses a ref to store the latest callback so the effect does NOT re-run
 * when the callback reference changes — preventing infinite re-render loops.
 */
export function useAutoRefresh(callback: () => void, intervalMs: number) {
  const savedCallback = useRef(callback)

  // Keep the ref current without triggering effect re-runs
  useEffect(() => {
    savedCallback.current = callback
  }, [callback])

  useEffect(() => {
    savedCallback.current()
    const id = setInterval(() => savedCallback.current(), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs])
}
