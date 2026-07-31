import { useState, useEffect, useCallback } from "react"
import Sidebar from "./components/Sidebar"
import Chat from "./components/Chat"
import StatusIndicator from "./components/StatusIndicator"
import ErrorBoundary from "./components/ErrorBoundary"
import { useChatStore } from "./store/chatStore"
import { useWebSocket } from "./hooks/useWebSocket"
import { apiClient } from "./api/client"
import type { Session } from "./types"

function AppContent() {
  const {
    sessions,
    activeSessionId,
    setActiveSession,
    addSession,
    removeSession,
    setConnectionStatus,
  } = useChatStore()

  const [apiBase, setApiBase] = useState<string>("http://127.0.0.1:8000")

  useEffect(() => {
    const initApi = async () => {
      if (window.athena) {
        const base = await window.athena.getApiBase()
        setApiBase(base)
        apiClient.setBaseUrl(base)
      }
    }
    initApi()
  }, [])

  useEffect(() => {
    const loadSessions = async () => {
      try {
        const list = await apiClient.listSessions()
        list.forEach((s: Session) => addSession(s))
      } catch {
        // backend not running — silent
      }
    }
    loadSessions()
  }, [addSession])

  const handleOpen = useCallback(() => {
    setConnectionStatus("connected")
  }, [setConnectionStatus])

  const handleClose = useCallback(() => {
    setConnectionStatus("disconnected")
  }, [setConnectionStatus])

  const handleError = useCallback(() => {
    setConnectionStatus("error")
  }, [setConnectionStatus])

  const { sendEvent } = useWebSocket({
    sessionId: activeSessionId,
    onOpen: handleOpen,
    onClose: handleClose,
    onError: handleError,
    apiBase,
  })

  const handleNewSession = useCallback(async () => {
    try {
      const session = await apiClient.createSession("New Session")
      addSession({
        id: session.id,
        title: session.title,
        status: session.status as Session["status"],
        created_at: session.created_at,
        updated_at: session.created_at,
      })
      setActiveSession(session.id)
      return session.id
    } catch (err) {
      console.error("Failed to create session:", err)
      return null
    }
  }, [addSession, setActiveSession])

  const handleDeleteSession = useCallback(
    async (sessionId: string) => {
      try {
        await apiClient.deleteSession(sessionId)
        removeSession(sessionId)
        if (activeSessionId === sessionId) {
          setActiveSession(null)
        }
      } catch (err) {
        console.error("Failed to delete session:", err)
      }
    },
    [activeSessionId, removeSession, setActiveSession],
  )

  const handleSelectSession = useCallback(
    (sessionId: string) => {
      setActiveSession(sessionId)
    },
    [setActiveSession],
  )

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Sidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        onSelectSession={handleSelectSession}
        onNewSession={handleNewSession}
        onDeleteSession={handleDeleteSession}
      />
      <main className="flex-1 flex flex-col min-w-0">
        <StatusIndicator />
        <Chat sendEvent={sendEvent} />
      </main>
    </div>
  )
}

function App() {
  return (
    <ErrorBoundary>
      <AppContent />
    </ErrorBoundary>
  )
}

export default App
