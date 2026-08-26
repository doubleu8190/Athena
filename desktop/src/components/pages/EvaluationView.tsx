import { useCallback, useEffect, useState } from "react"
import { AlertTriangle, BarChart3, Check, List, MessageCircle, RefreshCw, Settings2, Trash2 } from "lucide-react"
import { apiClient } from "../../api/client"
import type { EvaluationCaseSummary, EvaluationPage, EvaluationReport, EvaluationReportSummary, EvaluationSettings, FeedbackRating, FeedbackRecord, RetrievalEventDetail, RetrievalEventSummary } from "../../types"
import type { Column } from "../ui/DataTable"
import ViewShell from "./ViewShell"
import Toolbar from "../ui/Toolbar"
import DataTable from "../ui/DataTable"
import Badge from "../ui/Badge"
import Toggle from "../ui/Toggle"
import CorrectionDialog from "../evaluation/CorrectionDialog"

type EvaluationSection = "records" | "feedback" | "cases" | "reports" | "settings"

const SECTIONS: Array<{ id: EvaluationSection; label: string; icon: typeof List }> = [
  { id: "records", label: "记录", icon: List },
  { id: "feedback", label: "反馈", icon: MessageCircle },
  { id: "cases", label: "用例", icon: Check },
  { id: "reports", label: "报告", icon: BarChart3 },
  { id: "settings", label: "设置", icon: Settings2 },
]

function EvaluationView() {
  const [section, setSection] = useState<EvaluationSection>("records")
  const [refreshKey, setRefreshKey] = useState(0)
  const refresh = () => setRefreshKey((value) => value + 1)
  return <ViewShell side={<EvaluationNav section={section} onSelect={setSection} />}>
    {section === "records" && <RecordsView key={refreshKey} onRefresh={refresh} />}
    {section === "feedback" && <FeedbackView key={refreshKey} onRefresh={refresh} />}
    {section === "cases" && <CasesView key={refreshKey} onRefresh={refresh} />}
    {section === "reports" && <ReportsView key={refreshKey} onRefresh={refresh} />}
    {section === "settings" && <EvaluationSettingsView key={refreshKey} onRefresh={refresh} />}
  </ViewShell>
}

function EvaluationNav({ section, onSelect }: { section: EvaluationSection; onSelect: (section: EvaluationSection) => void }) {
  return <div className="p-4 space-y-4"><div><h2 className="text-sm font-semibold">检索评估</h2><p className="mt-1 text-xs text-athena-muted">本地记录与回归用例</p></div><div className="space-y-0.5">{SECTIONS.map(({ id, label, icon: Icon }) => <button key={id} type="button" onClick={() => onSelect(id)} className={`flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm ${section === id ? "bg-athena-accent/15 text-athena-accent font-medium" : "text-athena-muted hover:bg-athena-border/40 hover:text-athena-text"}`}><Icon className="h-4 w-4" />{label}</button>)}</div></div>
}

function RecordsView({ onRefresh }: { onRefresh: () => void }) {
  const [page, setPage] = useState<EvaluationPage<RetrievalEventSummary> | null>(null)
  const [source, setSource] = useState("")
  const [feedbackStatus, setFeedbackStatus] = useState("")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [detail, setDetail] = useState<RetrievalEventDetail | null>(null)
  const load = useCallback(async () => {
    setLoading(true)
    try { setPage(await apiClient.listEvaluationEvents({ source, feedback_status: feedbackStatus, limit: 50 })); setError(null) }
    catch (cause) { setError(cause instanceof Error ? cause.message : "加载检索记录失败") }
    finally { setLoading(false) }
  }, [feedbackStatus, source])
  useEffect(() => { void load() }, [load])
  const columns: Column<RetrievalEventSummary>[] = [
    { key: "created", header: "时间", className: "whitespace-nowrap", render: (row) => <span className="text-xs text-athena-muted">{formatDate(row.created_at)}</span> },
    { key: "query", header: "查询", className: "max-w-[360px]", render: (row) => <span className="block truncate" title={row.query}>{row.query}</span> },
    { key: "source", header: "来源", render: (row) => <Badge tone="accent">{row.source}</Badge> },
    { key: "count", header: "结果", render: (row) => row.result_count },
    { key: "duration", header: "耗时", render: (row) => row.total_duration_ms == null ? "—" : `${Math.round(row.total_duration_ms)} ms` },
    { key: "feedback", header: "反馈", render: (row) => <FeedbackBadge feedback={row.feedback} /> },
    { key: "action", header: "操作", render: (row) => <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={(event) => { event.stopPropagation(); void apiClient.getEvaluationEvent(row.event_id).then(setDetail).catch((cause) => setError(cause instanceof Error ? cause.message : "加载详情失败")) }}>查看</button> },
  ]
  return <><Toolbar title="检索记录" subtitle={page ? `共 ${page.total} 条本地记录` : "回顾真实检索"} actions={<button type="button" title="刷新" aria-label="刷新" onClick={() => { onRefresh(); void load() }} className="btn-ghost h-8 w-8 p-0"><RefreshCw className="h-4 w-4" /></button>} /><div className="space-y-4 p-6">
    <div className="flex flex-wrap gap-2"><select value={source} onChange={(event) => setSource(event.target.value)} className="input w-auto py-1.5 text-xs"><option value="">全部来源</option><option value="memory">memory</option><option value="file">file</option></select><select value={feedbackStatus} onChange={(event) => setFeedbackStatus(event.target.value)} className="input w-auto py-1.5 text-xs"><option value="">全部反馈</option><option value="accepted">accepted</option><option value="rejected">rejected</option><option value="corrected">corrected</option></select></div>
    {error ? <ErrorState message={error} onRetry={load} /> : <DataTable columns={columns} rows={page?.items ?? []} rowKey={(row) => row.event_id} loading={loading} emptyText="暂无检索记录" onRowClick={(row) => { void apiClient.getEvaluationEvent(row.event_id).then(setDetail).catch(() => undefined) }} />}
    {detail && <EventDetail detail={detail} onClose={() => setDetail(null)} />}
  </div></>
}

function FeedbackView({ onRefresh }: { onRefresh: () => void }) {
  const [page, setPage] = useState<EvaluationPage<FeedbackRecord> | null>(null)
  const [rating, setRating] = useState("rejected")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [dialogEventId, setDialogEventId] = useState<string | null>(null)
  const load = useCallback(async () => { setLoading(true); try { setPage(await apiClient.listEvaluationFeedback({ rating: rating ? rating as FeedbackRating : undefined, limit: 50 })); setError(null) } catch (cause) { setError(cause instanceof Error ? cause.message : "加载反馈失败") } finally { setLoading(false) } }, [rating])
  useEffect(() => { void load() }, [load])
  const columns: Column<FeedbackRecord>[] = [
    { key: "created", header: "时间", render: (row) => <span className="text-xs text-athena-muted">{formatDate(row.created_at)}</span> },
    { key: "event", header: "检索事件", render: (row) => <span className="font-mono text-xs">{row.event_id}</span> },
    { key: "rating", header: "状态", render: (row) => <FeedbackBadge feedback={row} /> },
    { key: "correct", header: "正确结果", render: (row) => row.expect_empty ? "应为空" : row.correct_result_ids.length ? `${row.correct_result_ids.length} 项` : <span className="text-athena-warning">待补充</span> },
    { key: "case", header: "用例", render: (row) => row.promoted_case_id ? <Badge tone="success">已发布</Badge> : row.rating === "rejected" ? <span className="text-xs text-athena-muted">需补充标准答案</span> : <span className="text-xs text-athena-muted">可发布</span> },
    { key: "action", header: "操作", render: (row) => <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={() => setDialogEventId(row.event_id)}>{row.rating === "rejected" ? "补充修正" : "查看修正"}</button> },
  ]
  return <><Toolbar title="待确认反馈" subtitle="无用反馈不会自动成为评估用例" actions={<button type="button" title="刷新" aria-label="刷新" onClick={() => { onRefresh(); void load() }} className="btn-ghost h-8 w-8 p-0"><RefreshCw className="h-4 w-4" /></button>} /><div className="space-y-4 p-6"><div className="flex gap-2"><select value={rating} onChange={(event) => setRating(event.target.value)} className="input w-auto py-1.5 text-xs"><option value="rejected">仅无用</option><option value="corrected">已修正</option><option value="">全部</option></select></div>{error ? <ErrorState message={error} onRetry={load} /> : <DataTable columns={columns} rows={page?.items ?? []} rowKey={(row) => row.feedback_id} loading={loading} emptyText="暂无待确认反馈" />}{dialogEventId && <CorrectionDialog eventIds={[dialogEventId]} onClose={() => setDialogEventId(null)} onSaved={() => { setDialogEventId(null); onRefresh() }} />}</div></>
}

function CasesView({ onRefresh }: { onRefresh: () => void }) {
  const [page, setPage] = useState<EvaluationPage<EvaluationCaseSummary> | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(async () => { setLoading(true); try { setPage(await apiClient.listEvaluationCases({ limit: 50 })); setError(null) } catch (cause) { setError(cause instanceof Error ? cause.message : "加载评估用例失败") } finally { setLoading(false) } }, [])
  useEffect(() => { void load() }, [load])
  const columns: Column<EvaluationCaseSummary>[] = [
    { key: "case", header: "用例", render: (row) => <span className="font-mono text-xs">{row.case_id}</span> },
    { key: "source", header: "来源", render: (row) => <Badge tone="accent">{row.source}</Badge> },
    { key: "labels", header: "标签", render: (row) => <span className="text-xs text-athena-muted">{row.labels.join(" · ") || "—"}</span> },
    { key: "version", header: "数据集", render: (row) => <span className="font-mono text-xs">{row.dataset_version}</span> },
    { key: "status", header: "状态", render: (row) => <Badge tone={row.status === "active" ? "success" : "warning"}>{row.status}</Badge> },
    { key: "created", header: "创建时间", render: (row) => <span className="text-xs text-athena-muted">{formatDate(row.created_at)}</span> },
  ]
  return <><Toolbar title="评估用例" subtitle={page ? `${page.total} 个已发布用例` : "可重放的本地金标"} actions={<button type="button" title="刷新" aria-label="刷新" onClick={() => { onRefresh(); void load() }} className="btn-ghost h-8 w-8 p-0"><RefreshCw className="h-4 w-4" /></button>} /><div className="p-6">{error ? <ErrorState message={error} onRetry={load} /> : <DataTable columns={columns} rows={page?.items ?? []} rowKey={(row) => row.case_id} loading={loading} emptyText="暂无已发布用例" />}</div></>
}

function ReportsView({ onRefresh }: { onRefresh: () => void }) {
  const [page, setPage] = useState<EvaluationPage<EvaluationReportSummary> | null>(null)
  const [report, setReport] = useState<EvaluationReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(async () => { setLoading(true); try { setPage(await apiClient.listEvaluationReports({ limit: 50 })); setError(null) } catch (cause) { setError(cause instanceof Error ? cause.message : "加载评估报告失败") } finally { setLoading(false) } }, [])
  useEffect(() => { void load() }, [load])
  const columns: Column<EvaluationReportSummary>[] = [
    { key: "created", header: "时间", render: (row) => <span className="text-xs text-athena-muted">{formatDate(row.created_at)}</span> },
    { key: "kind", header: "类型", render: (row) => <Badge tone="accent">{row.kind}</Badge> },
    { key: "dataset", header: "数据集", render: (row) => <span className="font-mono text-xs">{row.dataset_version || "—"}</span> },
    { key: "model", header: "模型", render: (row) => row.model || "—" },
    { key: "report", header: "报告", render: (row) => <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={() => void apiClient.getEvaluationReport(row.report_id).then(setReport).catch((cause) => setError(cause instanceof Error ? cause.message : "加载报告详情失败"))}>查看</button> },
  ]
  return <><Toolbar title="评估报告" subtitle="仅查看后端已生成的 run / comparison 结果" actions={<button type="button" title="刷新" aria-label="刷新" onClick={() => { onRefresh(); void load() }} className="btn-ghost h-8 w-8 p-0"><RefreshCw className="h-4 w-4" /></button>} /><div className="space-y-4 p-6">{error ? <ErrorState message={error} onRetry={load} /> : <DataTable columns={columns} rows={page?.items ?? []} rowKey={(row) => row.report_id} loading={loading} emptyText="暂无离线评估报告" />}{report && <ReportDetail report={report} onClose={() => setReport(null)} />}</div></>
}

function EvaluationSettingsView({ onRefresh }: { onRefresh: () => void }) {
  const [settings, setSettings] = useState<EvaluationSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [sampleRate, setSampleRate] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(async () => { setLoading(true); try { setSettings(await apiClient.getEvaluationSettings()); setError(null) } catch (cause) { setError(cause instanceof Error ? cause.message : "加载评估设置失败") } finally { setLoading(false) } }, [])
  useEffect(() => { void load() }, [load])
  useEffect(() => { if (settings) setSampleRate(settings.record_sample_rate) }, [settings])
  const setEnabled = async (enabled: boolean) => { setSaving(true); try { setSettings(await apiClient.updateEvaluationSettings({ record_enabled: enabled })) } catch (cause) { setError(cause instanceof Error ? cause.message : "更新设置失败") } finally { setSaving(false) } }
  const saveSampleRate = async () => { setSaving(true); try { setSettings(await apiClient.updateEvaluationSettings({ record_sample_rate: sampleRate })) } catch (cause) { setError(cause instanceof Error ? cause.message : "更新采样率失败") } finally { setSaving(false) } }
  const clear = async (target: "records" | "feedback" | "cases" | "reports", label: string) => { if (!window.confirm(`将清空${label}，此操作不可撤销。是否继续？`)) return; try { await apiClient.clearEvaluationRecords([target]); await load(); onRefresh() } catch (cause) { setError(cause instanceof Error ? cause.message : "清理失败") } }
  return <><Toolbar title="评估设置" subtitle="管理本地检索记录与数据清理" /><div className="max-w-3xl space-y-6 p-6">{error ? <ErrorState message={error} onRetry={load} /> : loading ? <div className="text-sm text-athena-muted">加载中…</div> : settings && <><div className="space-y-4 rounded-lg border border-athena-border bg-athena-surface p-4"><div className="flex items-center justify-between"><div><div className="text-sm font-medium">记录检索事件</div><div className="mt-1 text-xs text-athena-muted">关闭后，用户主动反馈仍可由后端补写事件。</div></div><Toggle checked={settings.record_enabled} disabled={saving} onChange={setEnabled} label="记录检索事件" /></div><div><div className="flex items-center justify-between text-xs text-athena-muted"><span>采样率</span><span className="font-mono text-athena-text">{Math.round(sampleRate * 100)}%</span></div><div className="mt-2 flex items-center gap-3"><input type="range" min="0" max="1" step="0.05" value={sampleRate} onChange={(event) => setSampleRate(Number(event.target.value))} className="w-full accent-athena-accent" disabled={saving} /><button type="button" className="btn-secondary px-2 py-1 text-xs" onClick={() => void saveSampleRate()} disabled={saving || sampleRate === settings.record_sample_rate}>保存</button></div></div><div><div className="text-xs text-athena-muted">数据目录</div><div className="mt-1 break-all font-mono text-xs">{settings.data_directory}</div></div></div><div className="grid grid-cols-2 gap-3 sm:grid-cols-4">{Object.entries(settings.counts).map(([key, value]) => <div key={key} className="rounded-lg border border-athena-border bg-athena-surface p-3"><div className="text-xs text-athena-muted">{countLabel(key)}</div><div className="mt-1 text-xl font-semibold">{value}</div></div>)}</div><div className="rounded-lg border border-athena-danger/30 bg-athena-danger/5 p-4"><div className="flex items-start gap-2"><AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0 text-athena-warning" /><div><div className="text-sm font-medium">本地数据清理</div><div className="mt-1 text-xs text-athena-muted">每个目标单独确认；已发布用例默认不会被连带删除。</div></div></div><div className="mt-4 grid gap-2 sm:grid-cols-2">{([["records", "检索记录"], ["feedback", "反馈"], ["cases", "评估用例"], ["reports", "评估报告"]] as const).map(([key, label]) => <button key={key} type="button" onClick={() => void clear(key, label)} className="btn-secondary justify-start text-xs"><Trash2 className="h-3.5 w-3.5 text-athena-danger" />清空{label}</button>)}</div></div></>}</div></>
}

function EventDetail({ detail, onClose }: { detail: RetrievalEventDetail; onClose: () => void }) { return <div className="border border-athena-border bg-athena-surface p-4"><div className="flex items-start justify-between"><div><div className="text-xs text-athena-muted">{detail.source} · {detail.event_id}</div><h2 className="mt-1 text-sm font-semibold break-words">{detail.query}</h2></div><button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={onClose}>关闭</button></div><div className="mt-4 space-y-2">{detail.results.map((result) => <div key={result.id} className="border-l-2 border-athena-accent/50 pl-3"><div className="text-sm">{result.title || result.id}</div><div className="mt-1 text-xs text-athena-muted">{result.summary || result.content || "无摘要"}</div></div>)}</div></div> }
function ReportDetail({ report, onClose }: { report: EvaluationReport; onClose: () => void }) { const baseline = report.baseline ?? report.metrics ?? {}; const candidate = report.candidate ?? {}; const corrected = report.corrected_metrics ?? {}; const keys = Array.from(new Set([...Object.keys(baseline), ...Object.keys(candidate), ...Object.keys(corrected)])); return <div className="border border-athena-border bg-athena-surface p-4"><div className="flex items-center justify-between"><div><h2 className="text-sm font-semibold">报告详情</h2><p className="mt-1 text-xs text-athena-muted">{report.dataset_version || "未提供数据集版本"} · {report.model || "未提供模型"}</p></div><button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={onClose}>关闭</button></div><div className="mt-4 overflow-x-auto"><table className="w-full text-sm"><thead><tr className="border-b border-athena-border text-left text-xs text-athena-muted"><th className="px-2 py-2">指标</th><th className="px-2 py-2">Baseline</th><th className="px-2 py-2">Candidate</th><th className="px-2 py-2">Delta</th><th className="px-2 py-2">user-corrected</th></tr></thead><tbody>{keys.map((key) => { const before = baseline[key] ?? null; const after = candidate[key] ?? null; const delta = before != null && after != null ? after - before : null; return <tr key={key} className="border-b border-athena-border/50"><td className="px-2 py-2">{key}</td><td className="px-2 py-2 font-mono">{formatMetric(before)}</td><td className="px-2 py-2 font-mono">{formatMetric(after)}</td><td className={`px-2 py-2 font-mono ${delta != null && delta < 0 ? "text-athena-danger" : "text-athena-success"}`}>{formatMetric(delta)}</td><td className="px-2 py-2 font-mono">{formatMetric(corrected[key])}</td></tr> })}</tbody></table></div></div> }
function FeedbackBadge({ feedback }: { feedback?: FeedbackRecord | null }) { if (!feedback) return <span className="text-xs text-athena-muted">未反馈</span>; const tone = feedback.rating === "accepted" ? "success" : feedback.rating === "rejected" ? "danger" : "accent"; return <Badge tone={tone}>{feedback.rating}</Badge> }
function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) { return <div className="flex items-center justify-between border border-athena-danger/30 bg-athena-danger/5 px-3 py-3 text-sm text-athena-danger"><span>{message}</span><button type="button" onClick={() => void onRetry()} className="btn-ghost px-2 py-1 text-xs text-athena-danger">重试</button></div> }
function formatDate(value: string) { try { return new Date(value).toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) } catch { return value } }
function formatMetric(value: number | null | undefined) { return value == null ? "—" : value.toFixed(3) }
function countLabel(key: string) { return ({ records: "记录", feedback: "反馈", cases: "用例", reports: "报告" } as Record<string, string>)[key] ?? key }

export default EvaluationView
