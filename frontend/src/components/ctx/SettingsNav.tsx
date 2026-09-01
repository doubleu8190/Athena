interface Section {
  id: string
  label: string
}

const SECTIONS: Section[] = [
  { id: "server", label: "服务器" },
  { id: "database", label: "存储" },
  { id: "harness", label: "执行引擎" },
  { id: "llm", label: "LLM 参数" },
  { id: "memory", label: "记忆" },
  { id: "context", label: "上下文压缩" },
  { id: "sandbox", label: "沙箱" },
  { id: "approval", label: "审批" },
]

interface SettingsNavProps {
  activeSection: string
  onSelect: (sectionId: string) => void
}

function SettingsNav({ activeSection, onSelect }: SettingsNavProps) {
  return (
    <div className="p-4 space-y-4">
      <h2 className="text-sm font-semibold text-athena-text">系统设置</h2>
      <div className="space-y-0.5">
        {SECTIONS.map((s) => {
          const active = activeSection === s.id
          return (
            <button
              key={s.id}
              onClick={() => onSelect(s.id)}
              className={`w-full text-left px-3 py-1.5 rounded-lg text-sm transition-colors ${
                active
                  ? "bg-athena-accent/15 text-athena-accent font-medium"
                  : "text-athena-muted hover:text-athena-text hover:bg-athena-border/40"
              }`}
            >
              {s.label}
            </button>
          )
        })}
      </div>
    </div>
  )
}

export default SettingsNav
export { SECTIONS }
