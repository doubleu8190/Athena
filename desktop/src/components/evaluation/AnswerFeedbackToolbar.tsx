import { Check, Loader2, PencilLine, ThumbsDown, ThumbsUp } from "lucide-react"
import type { FeedbackRecord, FeedbackRating, RetrievalReference } from "../../types"

interface AnswerFeedbackToolbarProps {
  retrievalEvents: RetrievalReference[]
  feedback: Record<string, FeedbackRecord>
  submitting: boolean
  submittingRating: "accepted" | "rejected" | null
  error: string | null
  onSubmit: (rating: Exclude<FeedbackRating, "corrected">) => void
  onCorrect: () => void
}

function AnswerFeedbackToolbar({
  retrievalEvents,
  feedback,
  submitting,
  submittingRating,
  error,
  onSubmit,
  onCorrect,
}: AnswerFeedbackToolbarProps) {
  if (retrievalEvents.length === 0) return null

  const ratings = retrievalEvents
    .map((event) => feedback[event.event_id]?.rating)
    .filter((rating): rating is FeedbackRating => Boolean(rating))
  const currentRating = ratings.length > 0 && ratings.every((rating) => rating === ratings[0])
    ? ratings[0]
    : undefined

  const buttonClass = (active: boolean, tone: string) => `p-1.5 rounded-md transition-colors ${
    active ? tone : "text-athena-muted hover:text-athena-text hover:bg-athena-surface"
  }`

  return (
    <div className="flex min-h-7 items-center gap-1 text-athena-muted" aria-label="回答反馈">
      <button
        type="button"
        title="有用"
        aria-label="有用"
        disabled={submitting}
        onClick={() => onSubmit("accepted")}
        className={buttonClass(currentRating === "accepted", "bg-athena-success/15 text-athena-success")}
      >
        {submittingRating === "accepted" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ThumbsUp className="h-3.5 w-3.5" />}
      </button>
      <button
        type="button"
        title="无用"
        aria-label="无用"
        disabled={submitting}
        onClick={() => onSubmit("rejected")}
        className={buttonClass(currentRating === "rejected", "bg-athena-danger/15 text-athena-danger")}
      >
        {submittingRating === "rejected" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ThumbsDown className="h-3.5 w-3.5" />}
      </button>
      <button
        type="button"
        title={currentRating === "corrected" ? "修改修正" : "修正检索结果"}
        aria-label={currentRating === "corrected" ? "修改修正" : "修正检索结果"}
        disabled={submitting}
        onClick={onCorrect}
        className={buttonClass(currentRating === "corrected", "bg-athena-accent/15 text-athena-accent")}
      >
        <PencilLine className="h-3.5 w-3.5" />
      </button>
      {currentRating && <span title="已记录" aria-label="已记录"><Check className="ml-1 h-3.5 w-3.5 text-athena-success" /></span>}
      {error && <span className="ml-1 max-w-[240px] truncate text-[11px] text-athena-danger" title={error}>{error}</span>}
    </div>
  )
}

export default AnswerFeedbackToolbar
