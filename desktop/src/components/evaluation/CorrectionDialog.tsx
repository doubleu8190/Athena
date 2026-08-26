import { useEffect, useState } from "react"
import { Loader2, X } from "lucide-react"
import { apiClient } from "../../api/client"
import type { FeedbackRecord, RetrievalEventDetail, RetrievalResult } from "../../types"

interface CorrectionDialogProps {
  eventIds: string[]
  initialEventId?: string
  onClose: () => void
  onSaved: (feedback: FeedbackRecord) => void
}

function CorrectionDialog({ eventIds, initialEventId, onClose, onSaved }: CorrectionDialogProps) {
  const [selectedEventId, setSelectedEventId] = useState(initialEventId ?? (eventIds.length === 1 ? eventIds[0] : ""))
  const [detail, setDetail] = useState<RetrievalEventDetail | null>(null)
  const [selectedResultId, setSelectedResultId] = useState("")
  const [expectEmpty, setExpectEmpty] = useState(false)
  const [gain, setGain] = useState<1 | 2 | 3>(3)
  const [comment, setComment] = useState("")
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [promoting, setPromoting] = useState(false)
  const [saved, setSaved] = useState<FeedbackRecord | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    setDetail(null)
    setSelectedResultId("")
    if (!selectedEventId) {
      setLoading(false)
      return () => { cancelled = true }
    }
    apiClient.getEvaluationEvent(selectedEventId)
      .then((value) => {
        if (!cancelled) {
          setDetail(value)
          const existing = value.feedback
          setExpectEmpty(existing?.expect_empty === true)
          setGain(existing?.gain ?? 3)
          setComment(existing?.comment ?? "")
          setSelectedResultId(existing?.correct_result_ids[0] ?? "")
        }
      })
      .catch((cause) => { if (!cancelled) setError(cause instanceof Error ? cause.message : "无法加载检索记录") })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [selectedEventId])

  const submit = async () => {
    if (!selectedEventId || (!expectEmpty && !selectedResultId)) return
    setSaving(true)
    setError(null)
    try {
      const feedback = await apiClient.submitFeedback({
        event_id: selectedEventId,
        rating: "corrected",
        correct_result_ids: expectEmpty ? [] : [selectedResultId],
        expect_empty: expectEmpty,
        gain,
        comment: comment.trim() || undefined,
      })
      setSaved(feedback)
      onSaved(feedback)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存修正失败")
    } finally {
      setSaving(false)
    }
  }

  const promote = async () => {
    if (!saved || !saved.can_promote) return
    setPromoting(true)
    setError(null)
    try {
      const result = await apiClient.promoteEvaluationFeedback(saved.feedback_id)
      setSaved({ ...saved, promoted_case_id: result.case.case_id, can_promote: false })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "发布用例失败")
    } finally {
      setPromoting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" role="dialog" aria-modal="true" aria-labelledby="correction-title">
      <div className="w-full max-w-xl overflow-hidden rounded-lg border border-athena-border bg-athena-surface shadow-2xl">
        <div className="flex items-center justify-between border-b border-athena-border px-5 py-4">
          <div>
            <h2 id="correction-title" className="text-base font-semibold">修正检索结果</h2>
            <p className="mt-1 text-xs text-athena-muted">选择实际正确的结果，或标记这次查询应为空。</p>
          </div>
          <button type="button" onClick={onClose} title="关闭" aria-label="关闭" className="btn-ghost h-8 w-8 p-0"><X className="h-4 w-4" /></button>
        </div>

        <div className="max-h-[70vh] space-y-4 overflow-y-auto p-5">
          {eventIds.length > 1 && (
            <label className="block text-xs text-athena-muted">
              检索事件
              <select value={selectedEventId} onChange={(event) => setSelectedEventId(event.target.value)} className="input mt-1">
                {eventIds.map((eventId) => <option key={eventId} value={eventId}>{eventId}</option>)}
              </select>
            </label>
          )}
          {loading ? <div className="flex items-center gap-2 text-sm text-athena-muted"><Loader2 className="h-4 w-4 animate-spin" />加载候选结果…</div> : null}
          {error ? <div className="text-sm text-athena-danger">{error}</div> : null}
          {detail && (
            <>
              <div className="rounded-md border border-athena-border bg-athena-bg px-3 py-2">
                <div className="text-xs text-athena-muted">查询</div>
                <div className="mt-1 text-sm break-words">{detail.query}</div>
              </div>
              <label className="flex items-start gap-2 text-sm">
                <input type="checkbox" checked={expectEmpty} onChange={(event) => { setExpectEmpty(event.target.checked); if (event.target.checked) setSelectedResultId("") }} className="mt-0.5 accent-athena-accent" />
                <span><span className="text-athena-text">应为空</span><span className="ml-2 text-xs text-athena-muted">没有结果是正确答案</span></span>
              </label>
              {!expectEmpty && <ResultPicker results={detail.results} selectedId={selectedResultId} onSelect={setSelectedResultId} />}
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <label className="text-xs text-athena-muted">相关性
                  <select value={gain} onChange={(event) => setGain(Number(event.target.value) as 1 | 2 | 3)} className="input mt-1">
                    <option value={3}>3 · 完全相关</option><option value={2}>2 · 部分相关</option><option value={1}>1 · 边缘相关</option>
                  </select>
                </label>
                <label className="text-xs text-athena-muted">备注
                  <input value={comment} onChange={(event) => setComment(event.target.value)} className="input mt-1" placeholder="可选说明" />
                </label>
              </div>
              {detail.source === "file" && selectedResultId && <LocatorHint result={detail.results.find((item) => item.id === selectedResultId)} />}
            </>
          )}
        </div>
        <div className="flex items-center justify-between border-t border-athena-border px-5 py-3">
          <div className="text-xs text-athena-muted">{saved?.promoted_case_id ? <span className="text-athena-success">已加入评估用例</span> : saved ? "修正已保存，可加入评估用例" : ""}</div>
          <div className="flex gap-2">
            <button type="button" onClick={onClose} className="btn-ghost">关闭</button>
            {saved?.can_promote && <button type="button" onClick={promote} disabled={promoting} className="btn-secondary">{promoting && <Loader2 className="h-4 w-4 animate-spin" />}加入评估用例</button>}
            {!saved?.promoted_case_id && <button type="button" onClick={submit} disabled={saving || loading || !detail || (!expectEmpty && !selectedResultId)} className="btn-primary">{saving && <Loader2 className="h-4 w-4 animate-spin" />}保存修正</button>}
          </div>
        </div>
      </div>
    </div>
  )
}

function ResultPicker({ results, selectedId, onSelect }: { results: RetrievalResult[]; selectedId: string; onSelect: (id: string) => void }) {
  if (results.length === 0) return <div className="border border-dashed border-athena-border px-3 py-4 text-center text-xs text-athena-muted">后端没有返回候选结果，请标记“应为空”。</div>
  return <div className="space-y-2"><div className="text-xs text-athena-muted">正确结果</div>{results.map((result) => <label key={result.id} className={`block cursor-pointer rounded-md border px-3 py-2 ${selectedId === result.id ? "border-athena-accent bg-athena-accent/10" : "border-athena-border hover:border-athena-muted"}`}><div className="flex gap-2"><input type="radio" name="correct-result" checked={selectedId === result.id} onChange={() => onSelect(result.id)} className="mt-1 accent-athena-accent" /><div className="min-w-0"><div className="truncate text-sm">{result.title || result.id}</div><div className="mt-1 line-clamp-2 text-xs text-athena-muted">{result.summary || result.content || "无摘要"}</div></div></div></label>)}</div>
}

function LocatorHint({ result }: { result?: RetrievalResult }) {
  if (!result?.locator) return null
  return <div className="text-xs text-athena-muted">文件定位：<span className="font-mono text-athena-text">{Object.entries(result.locator).map(([key, value]) => `${key}=${String(value)}`).join(" · ")}</span></div>
}

export default CorrectionDialog
