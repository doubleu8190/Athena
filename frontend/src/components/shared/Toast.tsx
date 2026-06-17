import { useEffect, useState, useCallback } from 'react'

interface ToastMessage {
  id: number
  message: string
  type: 'success' | 'error' | 'warning' | 'info'
}

let toastId = 0
let addToastFn: ((msg: Omit<ToastMessage, 'id'>) => void) | null = null

export function showToast(message: string, type: ToastMessage['type'] = 'info') {
  addToastFn?.({ message, type })
}

export default function Toast() {
  const [toasts, setToasts] = useState<ToastMessage[]>([])

  const addToast = useCallback((msg: Omit<ToastMessage, 'id'>) => {
    const id = ++toastId
    setToasts((prev) => [...prev, { ...msg, id }])
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id))
    }, 4000)
  }, [])

  useEffect(() => {
    addToastFn = addToast
    return () => { addToastFn = null }
  }, [addToast])

  const bgMap = {
    success: 'bg-success',
    error: 'bg-error',
    warning: 'bg-warning',
    info: 'bg-accent',
  }

  const iconMap = {
    success: '✓',
    error: '✕',
    warning: '⚠',
    info: 'ℹ',
  }

  if (toasts.length === 0) return null

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`animate-slide-up flex items-center gap-2 px-4 py-3 rounded-lg shadow-lg text-white text-sm min-w-[280px] ${bgMap[t.type]}`}
        >
          <span className="text-base font-bold">{iconMap[t.type]}</span>
          <span className="flex-1">{t.message}</span>
          <button
            onClick={() => setToasts((prev) => prev.filter((x) => x.id !== t.id))}
            className="text-white/70 hover:text-white ml-2"
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  )
}
