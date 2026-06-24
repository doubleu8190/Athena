import { useEffect, useState, useCallback, useRef } from 'react'
import { CheckCircle, XCircle, AlertTriangle, Info, X } from 'lucide-react'

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

const iconMap = {
  success: CheckCircle,
  error: XCircle,
  warning: AlertTriangle,
  info: Info,
}

const bgMap = {
  success: 'bg-success',
  error: 'bg-error',
  warning: 'bg-warning',
  info: 'bg-accent',
}

const progressColorMap = {
  success: 'bg-white/30',
  error: 'bg-white/30',
  warning: 'bg-white/30',
  info: 'bg-white/30',
}

function ToastItem({ toast: t, onDismiss }: { toast: ToastMessage; onDismiss: (id: number) => void }) {
  const [exiting, setExiting] = useState(false)
  const [paused, setPaused] = useState(false)
  const progressRef = useRef<HTMLDivElement>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  useEffect(() => {
    if (paused) return
    // Animate progress bar
    const startTime = Date.now()
    const duration = 4000

    const animate = () => {
      if (paused) return
      const elapsed = Date.now() - startTime
      const remaining = Math.max(0, 1 - elapsed / duration)
      if (progressRef.current) {
        progressRef.current.style.transform = `scaleX(${remaining})`
      }
      if (remaining > 0) {
        requestAnimationFrame(animate)
      }
    }
    requestAnimationFrame(animate)

    timerRef.current = setTimeout(() => {
      setExiting(true)
      setTimeout(() => onDismiss(t.id), 200)
    }, duration)

    return () => clearTimeout(timerRef.current)
  }, [t.id, onDismiss, paused])

  const Icon = iconMap[t.type]

  return (
    <div
      className={`${exiting ? 'opacity-0 translate-x-4' : 'animate-slide-up'} flex flex-col rounded-xl shadow-card text-white text-sm min-w-[300px] transition-all duration-200 overflow-hidden`}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
    >
      <div className={`flex items-center gap-2 px-4 py-3 ${bgMap[t.type]} bg-opacity-95`}>
        <Icon size={18} />
        <span className="flex-1">{t.message}</span>
        <button
          onClick={() => {
            setExiting(true)
            setTimeout(() => onDismiss(t.id), 200)
          }}
          className="text-white/70 hover:text-white ml-2"
          aria-label="Dismiss"
        >
          <X size={16} />
        </button>
      </div>
      {/* Progress bar */}
      <div className={`h-0.5 ${progressColorMap[t.type]}`}>
        <div
          ref={progressRef}
          className="h-full bg-white/50 origin-left"
          style={{ transform: 'scaleX(1)' }}
        />
      </div>
    </div>
  )
}

export default function Toast() {
  const [toasts, setToasts] = useState<ToastMessage[]>([])

  const addToast = useCallback((msg: Omit<ToastMessage, 'id'>) => {
    const id = ++toastId
    setToasts((prev) => [...prev, { ...msg, id }])
  }, [])

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  useEffect(() => {
    addToastFn = addToast
    return () => { addToastFn = null }
  }, [addToast])

  if (toasts.length === 0) return null

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
      {toasts.map((t) => (
        <ToastItem key={t.id} toast={t} onDismiss={dismiss} />
      ))}
    </div>
  )
}
