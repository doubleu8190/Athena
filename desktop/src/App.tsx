import { useState, useEffect, useCallback } from "react"
import Sidebar from "./components/Sidebar"
import Chat from "./components/Chat"
import StatusIndicator from "./components/StatusIndicator"
import ErrorBoundary from "./components/ErrorBoundary"
import NavRail from "./components/NavRail"
import PageView from "./components/pages/PageView"
import { useChatStore } from "./store/chatStore"
import { useWebSocket } from "./hooks/useWebSocket"
import { apiClient } from "./api/client"
import type { Session, AppView } from "./types"

function AppContent() {
  const {
    sessions,
    activeSessionId,
    setActiveSession,
    addSession,
    setSessions,
    removeSession,
    setConnectionStatus,
    activeView,
    setActiveView,
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
        // 整体替换而非逐条前插：后端已按 updated_at desc（最近活跃在前）排序，
        // 逐条 addSession 会前插倒转顺序，导致侧栏最旧会话排在最上
        setSessions(list)
      } catch {
        // backend not running — silent
      }
    }
    loadSessions()
  }, [setSessions])

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
    // 单一全局连接：管理视图也保持订阅活跃会话，
    // 后台 run 事件在浏览管理页时不丢失，切回聊天数据已是最新
    sessionId: activeSessionId,
    onOpen: handleOpen,
    onClose: handleClose,
    onError: handleError,
    apiBase,
  })

  const handleNewSession = useCallback(async () => {
    try {
      const now = new Date();
      const year = now.getFullYear();
      const month = String(now.getMonth() + 1).padStart(2, '0');
      const day = String(now.getDate()).padStart(2, '0');
      const hours = String(now.getHours()).padStart(2, '0');
      const minutes = String(now.getMinutes()).padStart(2, '0');
      const seconds = String(now.getSeconds()).padStart(2, '0');
      const milliseconds = String(now.getMilliseconds()).padStart(3, '0');
      const title = `Session ${year}.${month}.${day} ${hours}:${minutes}:${seconds},${milliseconds}`;
      const session = await apiClient.createSession(title)
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
      // 选中即视为最近活跃：把会话浮到列表顶部，
      // 后端 updated_at 只在消息落库时刷新，选中到下一次落库之间需要即时反映
      useChatStore.setState((state) => {
        if (state.sessions[0]?.id === sessionId) return {}
        const idx = state.sessions.findIndex((s) => s.id === sessionId)
        if (idx === -1) return {}
        const sessions = [...state.sessions]
        const [s] = sessions.splice(idx, 1)
        return { sessions: [s, ...sessions] }
      })
    },
    [setActiveSession],
  )

  const handleSelectView = useCallback((view: AppView) => {
    setActiveView(view)
  }, [])

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <NavRail activeView={activeView} onSelectView={handleSelectView} />
      {activeView === "chat" ? (
        <>
          <Sidebar
            sessions={sessions}
            activeSessionId={activeSessionId}
            onSelectSession={handleSelectSession}
            onNewSession={handleNewSession}
            onDeleteSession={handleDeleteSession}
          />
          <main className="flex-1 flex flex-col min-w-0 min-h-0">
            <StatusIndicator />
            <Chat sendEvent={sendEvent} />
          </main>
        </>
      ) : (
        <PageView view={activeView} />
      )}
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
