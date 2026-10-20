import { useCallback, useEffect, useState } from "react"
import { CheckCircle2, ChevronLeft, ChevronRight, FlaskConical, Loader2, Play, XCircle } from "lucide-react"
import { apiClient } from "../../api/client"
import type { EvaluationRunDetail, EvaluationRunSummary } from "../../types"
import ViewShell from "./ViewShell"
import Toolbar from "../ui/Toolbar"
import Badge from "../ui/Badge"
import StatCard from "../ui/StatCard"

const PAGE_SIZE = 20

function tone(status: string): "default" | "success" | "warning" | "danger" | "accent" {
  if (status === "passed") return "success"
  if (status === "failed") return "danger"
  if (status === "partial") return "warning"
  if (status === "running") return "accent"
  return "default"
}

function EvaluationView() {
  const [runs, setRuns] = useState<EvaluationRunSummary[]>([])
  const [detail, setDetail] = useState<EvaluationRunDetail | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [page, setPage] = useState(0)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedCase, setExpandedCase] = useState<string | null>(null)
  const [datasetPath, setDatasetPath] = useState("tests/retrieval_eval/chinese_cases.json")
  const [starting, setStarting] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const result = await apiClient.listEvaluationRuns({ limit: PAGE_SIZE, offset: page * PAGE_SIZE })
      setRuns(result.items); setTotal(result.total); setError(null)
      if (result.items.length && (!selected || !result.items.some((item) => item.run_id === selected))) setSelected(result.items[0].run_id)
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)) } finally { setLoading(false) }
  }, [page, selected])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    if (!selected) { setDetail(null); return }
    apiClient.getEvaluationRun(selected).then(setDetail).catch((cause) => setError(cause instanceof Error ? cause.message : String(cause)))
  }, [selected])

  useEffect(() => {
    if (!detail || !["queued", "running"].includes(detail.status)) return
    const source = new EventSource(apiClient.getEvaluationEventsUrl(detail.run_id), { withCredentials: true })
    const refresh = () => apiClient.getEvaluationRun(detail.run_id).then((value) => { setDetail(value); if (!["queued", "running"].includes(value.status)) source.close() }).catch(() => {})
    for (const eventName of ["evaluation.started", "evaluation.case_started", "evaluation.case_completed", "evaluation.case_failed", "evaluation.completed", "evaluation.failed"]) {
      source.addEventListener(eventName, refresh)
    }
    const timer = window.setInterval(() => { void refresh(); void load() }, 5000)
    return () => { source.close(); window.clearInterval(timer) }
  }, [detail?.run_id, detail?.status, load])

  const startRun = async () => {
    setStarting(true)
    try {
      const result = await apiClient.startEvaluationRun({ dataset_path: datasetPath, name: "retrieval-smoke", mode: "retrieval", evaluators: ["retrieval_metrics"], target: {} })
      setSelected(result.run_id)
      await load()
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setStarting(false)
    }
  }

  const metrics = detail?.metrics.filter((item) => item.case_id === null) ?? []
  const failed = detail?.cases.filter((item) => item.status === "failed").length ?? 0

  return <ViewShell side={<div className="p-4 space-y-4">
    <div><h2 className="font-semibold">评测实验</h2><p className="text-xs text-athena-muted mt-1">查看数据集运行、指标和回归门禁</p></div>
    {loading ? <div className="py-8 text-center text-athena-muted"><Loader2 className="w-5 h-5 animate-spin inline-block" /></div> : runs.length === 0 ? <div className="py-8 text-center text-xs text-athena-muted">暂无评测运行</div> : <div className="space-y-1 overflow-y-auto max-h-[calc(100vh-220px)]">{runs.map((run) => <button key={run.run_id} type="button" onClick={() => setSelected(run.run_id)} className={`w-full text-left rounded-lg border px-3 py-3 ${selected === run.run_id ? "border-athena-accent/60 bg-athena-accent/10" : "border-transparent hover:border-athena-border hover:bg-athena-border/20"}`}><div className="flex items-start gap-2"><FlaskConical className="mt-0.5 w-4 h-4 text-athena-accent" /><div className="min-w-0"><div className="text-sm truncate">{run.name}</div><div className="text-[11px] text-athena-muted mt-1">{run.dataset_version}</div><Badge tone={tone(run.status)}>{run.status}</Badge></div></div></button>)}</div>}
    <div className="flex items-center justify-between border-t border-athena-border pt-3"><span className="text-[11px] text-athena-muted">共 {total} 条</span><div className="flex gap-1"><button type="button" onClick={() => setPage((value) => Math.max(0, value - 1))} disabled={page === 0} className="btn-ghost p-1.5" title="上一页"><ChevronLeft className="w-4 h-4" /></button><button type="button" onClick={() => setPage((value) => value + 1)} disabled={(page + 1) * PAGE_SIZE >= total} className="btn-ghost p-1.5" title="下一页"><ChevronRight className="w-4 h-4" /></button></div></div>
  </div>}>
    <Toolbar title="评测实验" subtitle="对比检索质量、运行稳定性和回归门禁" />
    <div className="p-6 space-y-6"><div className="flex flex-wrap items-end gap-2"><label className="flex-1 min-w-64 text-xs text-athena-muted">数据集文件路径<input value={datasetPath} onChange={(event) => setDatasetPath(event.target.value)} className="input mt-1 w-full px-3 py-2 text-sm" /></label><button type="button" disabled={starting || !datasetPath.trim()} onClick={() => void startRun()} className="btn-primary flex items-center gap-2 px-3 py-2 text-sm disabled:opacity-50"><Play className="w-4 h-4" />{starting ? "提交中" : "启动检索评测"}</button></div>{error ? <div className="text-sm text-athena-danger">加载失败：{error}</div> : null}{detail ? <><div className="grid grid-cols-2 lg:grid-cols-4 gap-4"><StatCard label="案例数" value={detail.case_count} icon={FlaskConical} /><StatCard label="已完成" value={detail.completed_count} icon={CheckCircle2} /><StatCard label="失败数" value={failed} icon={XCircle} /><StatCard label="状态" value={detail.status} icon={detail.status === "passed" ? CheckCircle2 : XCircle} /></div><div className="card p-5"><div className="flex items-start justify-between"><div><h2 className="font-semibold">{detail.name}</h2><p className="text-xs text-athena-muted mt-1">{detail.dataset_version} · {detail.mode}</p></div><Badge tone={tone(detail.status)}>{detail.status}</Badge></div><div className="mt-5"><h3 className="text-sm font-semibold mb-2">汇总指标</h3><div className="grid grid-cols-1 md:grid-cols-2 gap-2">{metrics.map((metric) => <div key={metric.metric_name} className="flex justify-between rounded-lg bg-athena-bg px-3 py-2 text-xs"><span>{metric.metric_name}</span><span className="font-mono">{metric.value == null ? "—" : metric.value.toFixed(4)}</span></div>)}</div></div><div className="mt-5"><h3 className="text-sm font-semibold mb-2">门禁</h3><div className="flex flex-wrap gap-2">{detail.gates.map((gate) => <Badge key={gate.metric} tone={gate.status === "passed" ? "success" : "danger"}>{gate.metric}: {gate.status}</Badge>)}</div></div></div><div className="card overflow-hidden"><div className="px-4 py-3 border-b border-athena-border text-sm font-semibold">逐题结果</div><div className="divide-y divide-athena-border">{detail.cases.map((item) => <div key={item.case_id}><button type="button" onClick={() => setExpandedCase(expandedCase === item.case_id ? null : item.case_id)} className="w-full px-4 py-3 flex items-center gap-3 text-xs text-left hover:bg-athena-bg/40"><Badge tone={item.status === "completed" ? "success" : "danger"}>{item.status}</Badge><span className="font-mono">{item.case_id}</span><span className="text-athena-muted ml-auto">{item.duration_ms == null ? "—" : `${Math.round(item.duration_ms)} ms`}</span></button>{expandedCase === item.case_id ? <div className="px-4 pb-4 space-y-2 text-xs bg-athena-bg/30"><div className="text-athena-muted">排序结果：<span className="font-mono">{item.ranking.join(", ") || "—"}</span></div>{item.error ? <div className="text-athena-danger">{item.error}</div> : null}<pre className="rounded-lg bg-athena-bg p-3 whitespace-pre-wrap break-words">{item.output || "无输出"}</pre><div className="flex flex-wrap gap-2">{item.metrics.map((metric) => <Badge key={`${item.case_id}-${metric.metric_name}`} tone="default">{metric.metric_name}: {metric.value == null ? "—" : metric.value.toFixed(3)}</Badge>)}</div></div> : null}</div>)}</div></div></> : <div className="py-20 text-center text-athena-muted"><FlaskConical className="w-10 h-10 mx-auto mb-3 opacity-50" /><p className="text-sm">选择一条评测运行查看详情</p></div>}</div>
  </ViewShell>
}

export default EvaluationView
