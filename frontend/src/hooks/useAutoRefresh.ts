import { useEffect } from 'react'

export function useAutoRefresh(callback: () => void, intervalMs: number, enabled = true) {
  useEffect(() => {
    if (!enabled) return
    callback()
    const timer = setInterval(callback, intervalMs)
    return () => clearInterval(timer)
  }, [callback, intervalMs, enabled])
}
