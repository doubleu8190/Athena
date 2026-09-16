import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { AlertCircle, CheckCircle2, Database, FileCode2, FileSpreadsheet, FileText, FolderOpen, Link2, Loader2, Plus, RefreshCw, Trash2, UploadCloud, X } from "lucide-react"
import { apiClient } from "../../api/client"
import { useChatStore } from "../../store/chatStore"
import type { Attachment, KnowledgeBase, SupportedAttachmentTypes } from "../../types"
import Badge from "../ui/Badge"
import SectionCard from "../ui/SectionCard"
import Toolbar from "../ui/Toolbar"
import ViewShell from "./ViewShell"

const FALLBACK_EXTENSIONS = [".pdf", ".docx", ".txt", ".md", ".csv", ".xlsx", ".json", ".py", ".js", ".ts"]

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function fileIcon(filename: string) {
  const extension = filename.split(".").pop()?.toLowerCase()
  if (["xlsx", "xls", "csv"].includes(extension ?? "")) return FileSpreadsheet
  if (["py", "js", "ts", "tsx", "jsx", "json", "yaml", "yml"].includes(extension ?? "")) return FileCode2
  return FileText
}

function statusLabel(status: Attachment["status"]): { text: string; tone: "success" | "warning" | "danger" | "default" } {
  if (status === "ready") return { text: "可检索", tone: "success" }
  if (status === "processing" || status === "uploaded") return { text: "处理中", tone: "warning" }
  if (status === "failed") return { text: "处理失败", tone: "danger" }
  return { text: "已删除", tone: "default" }
}

function KnowledgeBaseView() {
  const activeSessionId = useChatStore((state) => state.activeSessionId)
  const sessions = useChatStore((state) => state.sessions)
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [documents, setDocuments] = useState<Attachment[]>([])
  const [boundIds, setBoundIds] = useState<Set<string>>(new Set())
  const [pendingFiles, setPendingFiles] = useState<File[]>([])
  const [supportedTypes, setSupportedTypes] = useState<SupportedAttachmentTypes | null>(null)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState("")
  const [isDragging, setIsDragging] = useState(false)
  const [isUploading, setIsUploading] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const selected = knowledgeBases.find((item) => item.id === selectedId) ?? null
  const activeSession = sessions.find((session) => session.id === activeSessionId) ?? null
  const extensions = supportedTypes?.extensions ?? FALLBACK_EXTENSIONS
  const isBound = selectedId ? boundIds.has(selectedId) : false

  const loadKnowledgeBases = useCallback(async () => {
    try {
      const items = await apiClient.listKnowledgeBases()
      setKnowledgeBases(items)
      setSelectedId((current) => current && items.some((item) => item.id === current) ? current : items[0]?.id ?? null)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : "知识库加载失败")
    } finally {
      setLoading(false)
    }
  }, [])

  const loadDocuments = useCallback(async () => {
    if (!selectedId) { setDocuments([]); return }
    try { setDocuments(await apiClient.listKnowledgeDocuments(selectedId)) } catch (err) { setError(err instanceof Error ? err.message : "文档加载失败") }
  }, [selectedId])

  useEffect(() => { void loadKnowledgeBases(); apiClient.getKnowledgeAttachmentTypes().then(setSupportedTypes).catch(() => undefined) }, [loadKnowledgeBases])
  useEffect(() => { setPendingFiles([]); void loadDocuments() }, [loadDocuments])
  useEffect(() => {
    if (!activeSessionId) { setBoundIds(new Set()); return }
    apiClient.listSessionKnowledgeBases(activeSessionId).then((items) => setBoundIds(new Set(items.map((item) => item.id)))).catch(() => setBoundIds(new Set()))
  }, [activeSessionId])
  useEffect(() => {
    if (!documents.some((item) => item.status === "uploaded" || item.status === "processing")) return
    const timer = window.setTimeout(() => { void loadDocuments(); void loadKnowledgeBases() }, 2000)
    return () => window.clearTimeout(timer)
  }, [documents, loadDocuments, loadKnowledgeBases])

  const addFiles = (files: File[]) => {
    if (!selectedId) { setError("请先创建或选择一个知识库"); return }
    const unsupported = files.filter((file) => !extensions.some((extension) => file.name.toLowerCase().endsWith(extension.toLowerCase())))
    if (unsupported.length) { setError(`暂不支持：${unsupported.map((file) => file.name).join("、")}`); return }
    setError(null)
    setPendingFiles((current) => {
      const keys = new Set(current.map((file) => `${file.name}-${file.size}-${file.lastModified}`))
      return [...current, ...files.filter((file) => !keys.has(`${file.name}-${file.size}-${file.lastModified}`))]
    })
  }

  const createKnowledgeBase = async () => {
    const name = newName.trim()
    if (!name) return
    try { const created = await apiClient.createKnowledgeBase(name); setKnowledgeBases((items) => [created, ...items]); setSelectedId(created.id); setNewName(""); setCreating(false); setError(null) } catch (err) { setError(err instanceof Error ? err.message : "知识库创建失败") }
  }

  const upload = async () => {
    if (!selectedId || !pendingFiles.length || isUploading) return
    setIsUploading(true)
    try { const created = await apiClient.uploadKnowledgeDocuments(selectedId, pendingFiles); setDocuments((items) => [...items, ...created]); setPendingFiles([]); setError(null); await loadKnowledgeBases() } catch (err) { setError(err instanceof Error ? err.message : "文档导入失败") } finally { setIsUploading(false) }
  }

  const toggleBinding = async () => {
    if (!activeSessionId || !selectedId) return
    try { await apiClient.setSessionKnowledgeBase(activeSessionId, selectedId, !isBound); setBoundIds((current) => { const next = new Set(current); if (isBound) next.delete(selectedId); else next.add(selectedId); return next }) } catch (err) { setError(err instanceof Error ? err.message : "会话绑定失败") }
  }

  const deleteDocument = async (document: Attachment) => {
    if (!selectedId || !window.confirm(`删除文档“${document.filename}”？`)) return
    try { await apiClient.deleteKnowledgeDocument(selectedId, document.id); setDocuments((items) => items.filter((item) => item.id !== document.id)); await loadKnowledgeBases() } catch (err) { setError(err instanceof Error ? err.message : "文档删除失败") }
  }

  const deleteKnowledgeBase = async () => {
    if (!selected || !window.confirm(`删除知识库“${selected.name}”及其全部文档？`)) return
    try { await apiClient.deleteKnowledgeBase(selected.id); const next = knowledgeBases.filter((item) => item.id !== selected.id); setKnowledgeBases(next); setSelectedId(next[0]?.id ?? null) } catch (err) { setError(err instanceof Error ? err.message : "知识库删除失败") }
  }

  const totalSize = useMemo(() => documents.reduce((sum, item) => sum + item.size_bytes, 0), [documents])
  const readyCount = documents.filter((item) => item.status === "ready").length

  return <ViewShell side={<div className="p-3 h-full flex flex-col">
    <div className="flex items-center justify-between px-1 mb-3"><div className="flex items-center gap-2"><Database className="w-4 h-4 text-athena-accent" /><h2 className="text-sm font-semibold">知识库</h2></div><button type="button" onClick={() => setCreating(true)} className="p-1.5 rounded-md text-athena-muted hover:text-athena-text hover:bg-athena-border/40" title="新建知识库"><Plus className="w-4 h-4" /></button></div>
    {creating ? <div className="mb-3 rounded-lg border border-athena-accent/40 bg-athena-bg p-2"><input autoFocus value={newName} onChange={(event) => setNewName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void createKnowledgeBase(); if (event.key === "Escape") setCreating(false) }} className="input px-2 py-1.5 text-xs" placeholder="知识库名称" /><div className="flex justify-end gap-1 mt-2"><button type="button" onClick={() => setCreating(false)} className="btn-ghost px-2 py-1 text-xs">取消</button><button type="button" onClick={() => void createKnowledgeBase()} className="btn-primary px-2 py-1 text-xs">创建</button></div></div> : null}
    <div className="space-y-1 overflow-y-auto">{knowledgeBases.map((item) => <button type="button" key={item.id} onClick={() => setSelectedId(item.id)} className={`w-full text-left rounded-lg px-3 py-2.5 transition-colors ${selectedId === item.id ? "bg-athena-accent/15 text-athena-text" : "text-athena-muted hover:bg-athena-border/30 hover:text-athena-text"}`}><div className="flex items-center gap-2"><FolderOpen className={`w-4 h-4 ${selectedId === item.id ? "text-athena-accent" : ""}`} /><span className="text-sm truncate flex-1">{item.name}</span><span className="text-[11px]">{item.document_count}</span></div>{item.description ? <p className="text-[11px] truncate mt-1 ml-6">{item.description}</p> : null}</button>)}{!loading && !knowledgeBases.length ? <p className="text-xs text-athena-muted text-center py-8">还没有知识库</p> : null}</div>
    <div className="mt-auto pt-4 border-t border-athena-border"><p className="text-[11px] text-athena-muted mb-2">当前对话</p><p className="text-xs truncate mb-2" title={activeSession?.title}>{activeSession?.title ?? "未选择会话"}</p><button type="button" disabled={!activeSessionId || !selectedId} onClick={() => void toggleBinding()} className={`w-full btn px-2 py-1.5 text-xs border ${isBound ? "bg-athena-success/10 border-athena-success/30 text-athena-success" : "bg-athena-bg border-athena-border text-athena-muted hover:text-athena-text"}`}><Link2 className="w-3.5 h-3.5" />{isBound ? "已在此对话启用" : "在此对话中启用"}</button></div>
  </div>}>
    <Toolbar title={selected?.name ?? "知识库"} subtitle={selected ? `${documents.length} 个文档 · ${readyCount} 个可检索 · ${formatBytes(totalSize)}` : "创建独立知识库后开始导入"} actions={<div className="flex items-center gap-1"><button type="button" onClick={() => { void loadDocuments(); void loadKnowledgeBases() }} className="btn-ghost px-2 py-1.5" title="刷新"><RefreshCw className="w-4 h-4" /></button>{selected ? <button type="button" onClick={() => void deleteKnowledgeBase()} className="btn-ghost px-2 py-1.5 text-athena-danger" title="删除知识库"><Trash2 className="w-4 h-4" /></button> : null}</div>} />
    <div className="p-6 max-w-5xl mx-auto w-full space-y-5">{error ? <div className="flex items-center gap-2 rounded-lg border border-athena-danger/30 bg-athena-danger/10 px-3 py-2 text-sm text-red-300"><AlertCircle className="w-4 h-4" /><span className="flex-1">{error}</span><button type="button" onClick={() => setError(null)}><X className="w-4 h-4" /></button></div> : null}
      {!selected ? <div className="card py-20 text-center"><Database className="w-10 h-10 mx-auto mb-3 text-athena-accent opacity-70" /><h2 className="font-medium mb-1">创建第一个知识库</h2><p className="text-sm text-athena-muted mb-4">文档只上传一次，即可授权给多个对话使用。</p><button type="button" onClick={() => setCreating(true)} className="btn-primary"><Plus className="w-4 h-4" />新建知识库</button></div> : <>
        <SectionCard title="导入文档" description="文档独立保存；在左侧将知识库启用到需要使用它的对话。"><div className={`border border-dashed rounded-xl p-7 text-center transition-colors ${isDragging ? "border-athena-accent bg-athena-accent/10" : "border-athena-border hover:border-athena-accent/60"}`} onDragOver={(event) => { event.preventDefault(); setIsDragging(true) }} onDragLeave={() => setIsDragging(false)} onDrop={(event) => { event.preventDefault(); setIsDragging(false); addFiles(Array.from(event.dataTransfer.files)) }}><UploadCloud className="w-9 h-9 mx-auto mb-3 text-athena-accent" /><p className="text-sm font-medium mb-1">拖拽文档到这里</p><p className="text-xs text-athena-muted mb-4">单文件最大 512 MB · {extensions.slice(0, 6).join("、")} 等</p><input ref={fileInputRef} type="file" multiple accept={extensions.join(",")} className="hidden" onChange={(event) => { addFiles(Array.from(event.target.files ?? [])); event.target.value = "" }} /><button type="button" onClick={() => fileInputRef.current?.click()} className="btn-secondary"><FolderOpen className="w-4 h-4" />选择文件</button></div>{pendingFiles.length ? <div className="mt-4 space-y-2"><div className="flex justify-between text-xs text-athena-muted"><span>待导入 {pendingFiles.length} 个文件</span><button type="button" onClick={() => setPendingFiles([])}>清空</button></div>{pendingFiles.map((file, index) => { const Icon = fileIcon(file.name); return <div key={`${file.name}-${file.lastModified}`} className="flex items-center gap-3 rounded-lg bg-athena-bg px-3 py-2"><Icon className="w-4 h-4 text-athena-accent" /><span className="flex-1 truncate text-sm">{file.name}</span><span className="text-xs text-athena-muted">{formatBytes(file.size)}</span><button type="button" onClick={() => setPendingFiles((items) => items.filter((_, itemIndex) => itemIndex !== index))}><X className="w-4 h-4 text-athena-muted" /></button></div> })}<div className="flex justify-end pt-2"><button type="button" onClick={() => void upload()} disabled={isUploading} className="btn-primary">{isUploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <UploadCloud className="w-4 h-4" />}{isUploading ? "正在导入" : "开始导入"}</button></div></div> : null}</SectionCard>
        <SectionCard title="知识库文档" description="处理完成后，所有已授权对话都能通过文件工具检索这些内容.">{!documents.length ? <div className="py-10 text-center text-sm text-athena-muted"><FileText className="w-8 h-8 mx-auto mb-2 opacity-50" />还没有导入文档</div> : <div className="divide-y divide-athena-border">{documents.map((document) => { const Icon = fileIcon(document.filename); const status = statusLabel(document.status); return <div key={document.id} className="flex items-center gap-3 py-3 first:pt-0 last:pb-0"><div className="w-9 h-9 rounded-lg bg-athena-bg flex items-center justify-center"><Icon className="w-4 h-4 text-athena-accent" /></div><div className="flex-1 min-w-0"><p className="text-sm truncate">{document.filename}</p><p className="text-xs text-athena-muted mt-0.5">{formatBytes(document.size_bytes)} · {document.adapter_name ?? "等待解析"}</p></div><Badge tone={status.tone}>{status.text}</Badge>{document.status === "ready" ? <CheckCircle2 className="w-4 h-4 text-athena-success" /> : null}<button type="button" onClick={() => void deleteDocument(document)} className="p-1.5 text-athena-muted hover:text-athena-danger" title="删除文档"><Trash2 className="w-4 h-4" /></button></div> })}</div>}</SectionCard>
      </>}
    </div>
  </ViewShell>
}

export default KnowledgeBaseView
