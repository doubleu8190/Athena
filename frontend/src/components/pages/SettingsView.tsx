import { useCallback, useEffect, useRef, useState } from "react"
import type { SettingsView as SettingsData } from "../../types"
import { apiClient } from "../../api/client"
import ViewShell from "./ViewShell"
import SettingsNav from "../ctx/SettingsNav"
import Toolbar from "../ui/Toolbar"
import SectionCard from "../ui/SectionCard"
import Badge from "../ui/Badge"

interface Field {
  key: string
  label: string
  type?: "bool"
}

const SECTIONS: { id: string; title: string; fields: Field[] }[] = [
  {
    id: "server",
    title: "服务器",
    fields: [
      { key: "host", label: "监听地址" },
      { key: "port", label: "端口" },
      { key: "debug", label: "调试模式", type: "bool" },
    ],
  },
  {
    id: "database",
    title: "存储",
    fields: [
      { key: "sqlite_db_path", label: "SQLite 数据库" },
      { key: "chromadb_path", label: "ChromaDB 路径" },
    ],
  },
  {
    id: "harness",
    title: "执行引擎",
    fields: [
      { key: "max_turns_per_run", label: "单次最大轮数" },
      { key: "retry_budget", label: "重试预算" },
      { key: "tool_timeout", label: "工具超时（秒）" },
      { key: "llm_stream_timeout", label: "LLM 流式超时（秒）" },
      { key: "approval_timeout", label: "审批超时（秒）" },
    ],
  },
  {
    id: "llm",
    title: "LLM 参数",
    fields: [
      { key: "llm_temperature", label: "全局温度" },
      { key: "llm_max_tokens", label: "最大输出 Token" },
    ],
  },
  {
    id: "memory",
    title: "记忆",
    fields: [
      { key: "memory_ttl_days", label: "记忆 TTL（天）" },
      { key: "memory_min_score", label: "检索最低分数" },
      { key: "summary_threshold", label: "摘要阈值" },
      { key: "memory_sync_interval", label: "同步间隔（秒）" },
    ],
  },
  {
    id: "context",
    title: "上下文压缩",
    fields: [
      { key: "max_context_tokens", label: "最大上下文 Token" },
      { key: "compression_threshold", label: "压缩阈值" },
      { key: "keep_recent_turns", label: "保留最近轮数" },
      { key: "max_summary_tokens", label: "最大摘要 Token" },
    ],
  },
  {
    id: "sandbox",
    title: "沙箱",
    fields: [
      { key: "sandbox_enabled", label: "启用沙箱", type: "bool" },
      { key: "sandbox_image", label: "沙箱镜像" },
      { key: "sandbox_network_disabled", label: "禁用网络", type: "bool" },
    ],
  },
  {
    id: "approval",
    title: "审批",
    fields: [
      { key: "approval_batch_mode", label: "批量模式" },
      { key: "approval_keyboard_shortcuts", label: "键盘快捷键", type: "bool" },
    ],
  },
]

function SettingsView() {
  const [settings, setSettings] = useState<SettingsData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [activeSection, setActiveSection] = useState("server")
  const refs = useRef<Record<string, HTMLDivElement | null>>({})

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      setSettings(await apiClient.getSettings())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    reload()
  }, [reload])

  const handleSelect = (sectionId: string) => {
    setActiveSection(sectionId)
    refs.current[sectionId]?.scrollIntoView({ behavior: "smooth", block: "start" })
  }

  const renderValue = (field: Field) => {
    if (!settings) return "—"
    const value = settings[field.key as keyof SettingsData]
    if (field.type === "bool") {
      return value ? (
        <Badge tone="success">开</Badge>
      ) : (
        <Badge>关</Badge>
      )
    }
    return <span className="font-mono text-sm">{String(value)}</span>
  }

  return (
    <ViewShell side={<SettingsNav activeSection={activeSection} onSelect={handleSelect} />}>
      <Toolbar
        title="系统设置"
        subtitle="启动时从配置加载 · 只读展示"
      />
      <div className="p-6 space-y-6 max-w-3xl">
        {error ? (
          <div className="text-sm text-athena-danger">加载失败：{error}</div>
        ) : loading ? (
          <div className="text-sm text-athena-muted">加载中…</div>
        ) : (
          SECTIONS.map((section) => (
            <div
              key={section.id}
              ref={(el) => {
                refs.current[section.id] = el
              }}
              className="scroll-mt-6"
            >
              <SectionCard title={section.title}>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-4">
                  {section.fields.map((field) => (
                    <div key={field.key}>
                      <div className="text-xs text-athena-muted">{field.label}</div>
                      <div className="mt-1">{renderValue(field)}</div>
                    </div>
                  ))}
                </div>
              </SectionCard>
            </div>
          ))
        )}
      </div>
    </ViewShell>
  )
}

export default SettingsView
